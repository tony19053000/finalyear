"""V0.17 G2 tester round 2: break the new safe-restore path (real bundled PG).

Derived from the G2-D1 fix contract: a restore that cannot finish never costs the
live database; leftovers of a crashed earlier restore are cleaned; connected
clients do not block the swap; the same dump twice and foreign schema versions
behave; ``rollback-failed.json`` lifecycle is robust to a damaged file.
"""

from __future__ import annotations

# ruff: noqa: ASYNC240
import asyncio
import json
import os
import resource
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from mf_fabric import seed_ca_dir
from mf_managed_pg import free_port, managed_pg_bin_dir, short_dir

pytest.importorskip("asyncpg", reason="needs the controller extra")

import asyncpg
from mf_controller_harness import make_settings

from meshforge.common.errors import MeshForgeError
from meshforge.controller.db_managed import ManagedDbError, ManagedDbReason, admin
from meshforge.controller.db_managed.backup import (
    read_rollback_failed,
    write_rollback_failed,
)
from meshforge.controller.db_managed.procs import data_dir_holders
from meshforge.controller.db_managed.runtime import cluster_for
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.slow,
    pytest.mark.timeout(500),
]


@pytest.fixture(scope="session")
def pg_bin() -> Path:
    return managed_pg_bin_dir()


@pytest.fixture
def db_dirs(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    root = tmp_path / "db root" / "db"
    with short_dir("mfpg-g2r2-") as sock:
        yield root, sock
    data = root / "data"
    left = data_dir_holders(data) if data.exists() else []
    assert left == [], f"postgres processes left behind: {left}"


def _settings(
    tmp_path: Path, pg_bin: Path, root: Path, sock: Path
) -> ControllerSettings:
    s = make_settings(
        DatabaseSettings(),
        db={
            "mode": "managed",
            "root": root,
            "socket_dir": sock,
            "bin_dir": pg_bin,
            "port": free_port(),
        },
    ).model_copy(update={"data_dir": tmp_path / "data"})
    seed_ca_dir(s.ca_dir)
    return s


async def _add_user(rt: ControllerRuntime, name: str) -> None:
    from meshforge.controller.store.models.auth import UserRole

    assert rt.users is not None
    await rt.users.create_user(name, pw_hash="x" * 20, role=UserRole.USER, actor="t")


async def _names(rt: ControllerRuntime) -> list[str]:
    assert rt.users is not None
    return sorted(u.name for u in await rt.users.list_users())


async def _seed(settings: ControllerSettings) -> Path:
    """Initialise the cluster with user 'asha'; return a dump holding only 'asha'."""
    async with ControllerRuntime.start(settings) as rt:
        await _add_user(rt, "asha")
    dump = Path((await asyncio.to_thread(admin.backup, settings, None))["path"])
    return dump


async def _super(cluster, database: str = "postgres") -> asyncpg.Connection:  # type: ignore[no-untyped-def]
    return await asyncpg.connect(
        host=str(cluster.layout.socket_dir), port=cluster.port,
        user=cluster.superuser,
        password=cluster._require_password().get_secret_value(),
        database=database, timeout=10,
    )  # fmt: skip


async def _app(cluster) -> asyncpg.Connection:  # type: ignore[no-untyped-def]
    pw = (cluster.layout.root / "app.password").read_text().strip()
    return await asyncpg.connect(
        host=str(cluster.layout.socket_dir), port=cluster.port,
        user=cluster.app_user, password=pw, database=cluster.database, timeout=10,
    )  # fmt: skip


async def _dbs(cluster) -> set[str]:  # type: ignore[no-untyped-def]
    c = await _super(cluster)
    try:
        return {r[0] for r in await c.fetch("SELECT datname FROM pg_database")}
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_restore_with_connected_clients_swaps_and_cuts_them(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    dump = await _seed(settings)
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        clients = [await _app(cluster) for _ in range(5)]
        # one holds an open transaction with a lock on a table
        await clients[0].execute("BEGIN")
        await clients[0].execute("LOCK TABLE app_user IN ACCESS EXCLUSIVE MODE")
        blocked = asyncio.ensure_future(  # waits on that lock: a stuck client
            clients[1].execute("DELETE FROM app_user WHERE name = 'asha'")
        )
        await asyncio.sleep(0.5)
        assert not blocked.done()
        await asyncio.wait_for(cluster.restore_database(dump), 120)
        with pytest.raises((asyncpg.PostgresError, asyncpg.InterfaceError, OSError)):
            await asyncio.wait_for(blocked, 30)
        for c in clients:
            with pytest.raises(
                (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)
            ):
                await c.fetchval("SELECT 1")
        fresh = await _app(cluster)
        try:
            assert [r[0] for r in await fresh.fetch("SELECT name FROM app_user")] == [
                "asha"
            ]
        finally:
            await fresh.close()
        dbs = await _dbs(cluster)
        assert not {d for d in dbs if "__" in d}, dbs
    finally:
        await cluster.stop()
    assert not data_dir_holders(root / "data")


@pytest.mark.asyncio
async def test_leftovers_of_a_crashed_restore_are_cleaned_and_do_not_block(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    dump = await _seed(settings)
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        name = cluster.database
        # junk, populated, with a live connection in each: both crashed states
        for left in (f"{name}__restore", f"{name}__previous"):
            await cluster.create_database(left)
        held = []
        for left in (f"{name}__restore", f"{name}__previous"):
            c = await _super(cluster, left)
            await c.execute("CREATE TABLE junk(x int)")
            held.append(c)
        await asyncio.wait_for(cluster.restore_database(dump), 120)
        dbs = await _dbs(cluster)
        assert name in dbs and not {d for d in dbs if "__" in d}, dbs
        fresh = await _app(cluster)
        try:
            assert await fresh.fetchval("SELECT count(*) FROM app_user") == 1
        finally:
            await fresh.close()
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_crash_after_swap_leaves_previous_and_next_start_still_works(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    """A crash between the swap and the final drop leaves <name>__previous."""
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    dump = await _seed(settings)
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        await cluster.create_database(f"{cluster.database}__previous")
    finally:
        await cluster.stop()
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["asha"]
        await _add_user(rt, "late")
    await asyncio.to_thread(admin.restore, settings, dump)
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["asha"]


@pytest.mark.asyncio
async def test_unreadable_dump_file_changes_nothing(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    dump = await _seed(settings)
    locked = tmp_path / "locked.dump"
    locked.write_bytes(dump.read_bytes())
    locked.chmod(0)
    try:
        with pytest.raises(ManagedDbError) as info:
            await asyncio.to_thread(admin.restore, settings, locked)
        assert info.value.reason is ManagedDbReason.RESTORE_FAILED
    finally:
        locked.chmod(0o600)
    assert not data_dir_holders(root / "data")
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["asha"]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", [0.3, 0.6, 0.9])
async def test_dump_corrupted_in_the_middle_never_half_applies(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path], where: float
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    dump = await _seed(settings)
    raw = bytearray(dump.read_bytes())
    at = int(len(raw) * where)
    for i in range(at, min(at + 64, len(raw))):
        raw[i] ^= 0xFF
    bad = tmp_path / "bitrot.dump"
    bad.write_bytes(bytes(raw))
    try:
        await asyncio.to_thread(admin.restore, settings, bad)
        outcome = "restored"
    except ManagedDbError as exc:
        reason = exc.reason
        assert reason is ManagedDbReason.RESTORE_FAILED
        outcome = "refused"
    assert not data_dir_holders(root / "data")
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        dbs = await _dbs(cluster)
        if outcome == "refused":
            # refused: live untouched; scratch cleaned
            assert not {d for d in dbs if "__" in d}, dbs
            c = await _app(cluster)
            try:
                assert [r[0] for r in await c.fetch("SELECT name FROM app_user")] == [
                    "asha"
                ]
            finally:
                await c.close()
        else:
            assert cluster.database in dbs
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_disk_write_failure_during_restore_keeps_live_data(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    """RLIMIT_FSIZE in the restoring process (and the postgres it starts) makes
    writing the second copy fail like a full disk does."""
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    async with ControllerRuntime.start(settings) as rt:
        await _add_user(rt, "asha")
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        c = await _app(cluster)
        try:
            await c.execute("CREATE TABLE bulk(x text)")
            await c.execute(
                "INSERT INTO bulk SELECT md5(i::text)||md5((i*7)::text)||md5((i*13)::text)"
                "||md5((i*17)::text) FROM generate_series(1, 600000) i"
            )
        finally:
            await c.close()
    finally:
        await cluster.stop()
    dump = Path((await asyncio.to_thread(admin.backup, settings, None))["path"])
    assert dump.stat().st_size > 24 * 1024 * 1024  # bigger than the write limit below
    d = settings.controller.db
    cfg = tmp_path / "controller.toml"
    cfg.write_text(
        f"data_dir = {str(settings.data_dir)!r}\n"
        '[controller.db]\nmode = "managed"\n'
        f"root = {str(d.root)!r}\nsocket_dir = {str(d.socket_dir)!r}\n"
        f"bin_dir = {str(d.bin_dir)!r}\nport = {d.port}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}

    def limited() -> None:
        lim = 24 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim, lim))

    res = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", "from meshforge.controller.app import main; main()",
         "db", "restore", str(dump), "--yes", "--config", str(cfg)],
        capture_output=True, text=True, timeout=400, env=env, preexec_fn=limited,
    )  # fmt: skip
    out = res.stdout + res.stderr
    assert "Traceback" not in out, out[-2500:]
    assert res.returncode == 4 and "File too large" in out, (
        res.returncode,
        out[-1500:],
    )
    await asyncio.sleep(1)
    assert not data_dir_holders(root / "data"), "postgres left running: " + out[-500:]
    # whatever happened, a normal start serves the original rows
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        c = await _app(cluster)
        try:
            assert await c.fetchval("SELECT count(*) FROM bulk") == 600000
            assert await c.fetchval("SELECT count(*) FROM app_user") == 1
        finally:
            await c.close()
        if res.returncode != 0:
            assert not {d for d in await _dbs(cluster) if "__restore" in d}
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_dump_of_a_future_schema_revision_is_refused_at_start_not_wiped(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    good = await _seed(settings)
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        c = await _super(cluster, cluster.database)
        try:
            await c.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        finally:
            await c.close()
        future = tmp_path / "future.dump"
        await cluster.dump(future)
    finally:
        await cluster.stop()
    await asyncio.to_thread(admin.restore, settings, future)  # data-only: allowed
    with pytest.raises(MeshForgeError):
        async with ControllerRuntime.start(settings):
            pytest.fail("started on a database from a newer version")
    assert not data_dir_holders(root / "data")
    # and the operator can go back
    await asyncio.to_thread(admin.restore, settings, good)
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["asha"]


@pytest.mark.asyncio
async def test_dump_of_an_empty_database_restores_and_the_controller_migrates(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    await _seed(settings)
    cluster = cluster_for(settings)
    await cluster.start()
    try:
        await cluster.create_database("scratchempty")
        empty = tmp_path / "empty-db.dump"
        await cluster.dump(empty, database="scratchempty")
    finally:
        await cluster.stop()
    await asyncio.to_thread(admin.restore, settings, empty)
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == []
        await _add_user(rt, "fresh")


# -------------------------------------------- rollback-failed record lifecycle
def test_damaged_rollback_record_is_still_reported_and_status_survives(
    tmp_path: Path,
) -> None:
    root = tmp_path
    path = root / "rollback-failed.json"
    for body in (b"", b"not json{{", b"[1,2]", b"\xff\xfe\x00", b"{" * 100_000):
        path.write_bytes(body)
        rec = read_rollback_failed(root)
        assert isinstance(rec, dict) and rec, body[:10]
    path.unlink()
    path.mkdir()  # a directory in its place
    assert isinstance(read_rollback_failed(root), dict)


def test_rollback_record_is_private_overwritten_and_truncates_error(
    tmp_path: Path,
) -> None:
    write_rollback_failed(
        tmp_path, backup=Path("/b/x ü.dump"), from_version="1", to_version="2",
        error="é" * 5000,
    )  # fmt: skip
    p = tmp_path / "rollback-failed.json"
    assert p.stat().st_mode & 0o777 == 0o600
    first = json.loads(p.read_text(encoding="utf-8"))
    assert len(first["error"]) <= 500
    write_rollback_failed(
        tmp_path,
        backup=Path("/b/y.dump"),
        from_version=None,
        to_version="3",
        error="e2",
    )
    assert json.loads(p.read_text())["backup"] == "/b/y.dump"
    assert not list(tmp_path.glob(".*tmp*")) and len(list(tmp_path.iterdir())) == 1


@pytest.mark.asyncio
async def test_failed_restore_does_not_clear_a_rollback_record_but_good_one_does(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock)
    dump = await _seed(settings)
    write_rollback_failed(
        root, backup=dump, from_version="1", to_version="2", error="boom"
    )
    bad = tmp_path / "bad.dump"
    bad.write_bytes(b"junk")
    with pytest.raises(ManagedDbError):
        await asyncio.to_thread(admin.restore, settings, bad)
    facts = await asyncio.to_thread(admin.status, settings)
    assert facts["rollback_failed"] is not None, "a failed restore cleared the record"
    await asyncio.to_thread(admin.restore, settings, dump)
    facts = await asyncio.to_thread(admin.status, settings)
    assert facts["rollback_failed"] is None
