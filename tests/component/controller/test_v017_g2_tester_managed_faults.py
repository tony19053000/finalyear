"""V0.17 G2 tester: failure injection on the managed PostgreSQL + backup/restore.

Real bundled PostgreSQL, real controller process / runtime, no mocks. From brief
5b items 1, 2, 4: postmaster death -> FAILURE (real process, non-zero exit, no
hang), secrets stay private and a missing/loosened secret never costs data,
restore as the app role (dump of the dev Compose database imports, Q21), a bad
dump never destroys the live database, two controllers never share a cluster.
"""

from __future__ import annotations

# ruff: noqa: ASYNC221, ASYNC240
import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from mf_fabric import seed_ca_dir
from mf_managed_pg import free_port, managed_pg_bin_dir, short_dir
from mf_session_client import poll_until

pytest.importorskip("asyncpg", reason="needs the controller extra")

import asyncpg
from mf_controller_harness import make_settings

from meshforge.common.errors import MeshForgeError
from meshforge.controller.db_managed import ManagedDbError, ManagedDbReason, admin
from meshforge.controller.db_managed.procs import data_dir_holders
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.slow,
    pytest.mark.asyncio,
    pytest.mark.timeout(400),
]


@pytest.fixture(scope="session")
def pg_bin() -> Path:
    return managed_pg_bin_dir()


@pytest.fixture
def db_dirs(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    root = tmp_path / "db root" / "db"  # a space in the path on purpose
    with short_dir("mfpg-g2t-") as sock:
        yield root, sock
    data = root / "data"
    left = data_dir_holders(data) if data.exists() else []
    assert left == [], f"postgres processes left behind: {left}"


def _settings(
    tmp_path: Path, pg_bin: Path, root: Path, sock: Path, port: int
) -> ControllerSettings:
    return make_settings(
        DatabaseSettings(),
        db={
            "mode": "managed",
            "root": root,
            "socket_dir": sock,
            "bin_dir": pg_bin,
            "port": port,
        },
    ).model_copy(update={"data_dir": tmp_path / "data"})


async def _add_user(runtime: ControllerRuntime, name: str) -> None:
    from meshforge.controller.store.models.auth import UserRole

    assert runtime.users is not None
    await runtime.users.create_user(
        name, pw_hash="x" * 20, role=UserRole.USER, actor="test"
    )


async def _names(runtime: ControllerRuntime) -> list[str]:
    assert runtime.users is not None
    return sorted(u.name for u in await runtime.users.list_users())


# ------------------------------------------------ real process, postmaster dies
def _config(tmp_path: Path, pg_bin: Path, root: Path, sock: Path) -> tuple[Path, int]:
    grpc_port, http_port, db_port = free_port(), free_port(), free_port()
    data_dir = tmp_path / "data"
    seed_ca_dir(data_dir / "controller" / "ca")
    cfg = tmp_path / "controller.toml"
    cfg.write_text(
        f"data_dir = {str(data_dir)!r}\n"
        '[log]\nformat = "json"\nlevel = "INFO"\n'
        "[controller]\n"
        f'grpc_bind = "127.0.0.1:{grpc_port}"\n'
        f'http_bind = "127.0.0.1:{http_port}"\n'
        "shutdown_drain_s = 1.0\n"
        "[controller.discovery]\nadvertise = false\n"
        '[controller.enroll]\nbind = "127.0.0.1:0"\n'
        '[controller.db]\nmode = "managed"\n'
        f"root = {str(root)!r}\nsocket_dir = {str(sock)!r}\n"
        f"bin_dir = {str(pg_bin)!r}\nport = {db_port}\n",
        encoding="utf-8",
    )
    return cfg, http_port


def _spawn(cfg: Path) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, "-c", "from meshforge.controller.app import main; main()",
         "--config", str(cfg)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip


def _wait_log(proc: subprocess.Popen[str], needle: str, within: float) -> list[str]:
    """Read the process's output until a line has ``needle`` (never blocks forever)."""
    import selectors

    assert proc.stdout is not None
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    lines: list[str] = []
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if sel.select(0.2):
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if needle in line:
                return lines
    raise AssertionError(f"{needle!r} not seen in {within}s:\n" + "".join(lines[-40:]))


async def test_postmaster_sigkill_under_a_real_controller_process_exits_nonzero(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    cfg, _http = _config(tmp_path, pg_bin, root, sock)
    proc = _spawn(cfg)
    try:
        await asyncio.to_thread(_wait_log, proc, "controller.listening", 90)
        holders = data_dir_holders(root / "data")
        assert holders
        postmaster = int((root / "data" / "postmaster.pid").read_text().split()[0])
        os.kill(postmaster, signal.SIGKILL)
        start = time.monotonic()
        code = await asyncio.to_thread(proc.wait, 30)
        took = time.monotonic() - start
        # systemd restarts on non-zero; 0 would leave a dead controller "done"
        assert code != 0, "controller exited 0 after its database died"
        assert code not in (4, 5), "exit 4/5 means RestartPreventExitStatus: no restart"
        assert took < 25
        rest = proc.stdout.read() if proc.stdout else ""
        assert "critical" in rest.lower() or "managed" in rest.lower(), rest[-2000:]
        assert "Traceback" not in rest, rest[-3000:]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        if proc.stdout:
            proc.stdout.close()
    # Backends of the killed postmaster are reaped by the next start; data survives.
    again = _spawn(cfg)
    try:
        await asyncio.to_thread(_wait_log, again, "controller.listening", 90)
        again.send_signal(signal.SIGTERM)
        assert again.wait(60) == 0
    finally:
        if again.poll() is None:
            again.kill()
            again.wait(10)
        if again.stdout:
            again.stdout.close()


async def test_sigterm_during_database_start_leaves_nothing_behind(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    cfg, _http = _config(tmp_path, pg_bin, root, sock)
    for delay in (0.05, 0.4, 1.0):
        proc = _spawn(cfg)
        try:
            await asyncio.sleep(delay)
            proc.send_signal(signal.SIGTERM)
            await asyncio.to_thread(proc.wait, 90)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
            if proc.stdout:
                proc.stdout.close()
        data = root / "data"
        if data.exists():

            async def gone(d: Path = data) -> bool:
                return not data_dir_holders(d)

            await poll_until(gone, timeout_s=30, interval_s=0.1)
    final = _spawn(cfg)  # after the interrupted starts, a normal start works
    try:
        await asyncio.to_thread(_wait_log, final, "controller.listening", 120)
        final.send_signal(signal.SIGTERM)
        assert final.wait(60) == 0
    finally:
        if final.poll() is None:
            final.kill()
            final.wait(10)
        if final.stdout:
            final.stdout.close()


# ---------------------------------------------------------------- secrets
async def test_loosened_password_file_is_refused_and_data_is_kept(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as rt:
        await _add_user(rt, "asha")
    for name in ("app.password", "admin.password"):
        path = root / name
        path.chmod(0o644)
        with pytest.raises(MeshForgeError):
            async with ControllerRuntime.start(settings):
                pytest.fail(f"started with a world-readable {name}")
        assert not data_dir_holders(root / "data")
        path.chmod(0o600)
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["asha"]


async def test_deleted_password_file_never_re_initialises_over_existing_data(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as rt:
        await _add_user(rt, "asha")
    marker = (root / "data" / "PG_VERSION").stat().st_ino
    (root / "app.password").unlink()
    try:
        async with ControllerRuntime.start(settings) as rt:
            # a controller that regenerated the secret must still hold the data
            assert await _names(rt) == ["asha"]
    except MeshForgeError:
        pass  # refusing is acceptable; wiping is not
    assert (root / "data" / "PG_VERSION").stat().st_ino == marker
    assert not data_dir_holders(root / "data")
    # whichever it chose, the data must still be reachable by a later start
    # once the operator did the right thing (we do not require auto-heal)
    assert (root / "data" / "base").is_dir()


# ------------------------------------------------------- two controllers
async def test_second_controller_on_the_same_cluster_is_refused_and_first_unharmed(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as first:
        await _add_user(first, "asha")
        other = settings.model_copy(update={"data_dir": tmp_path / "data2"})
        seed_ca_dir(other.ca_dir)
        with pytest.raises(MeshForgeError):
            async with ControllerRuntime.start(other):
                pytest.fail("two controllers on one cluster")
        # the first one still has its database and still serves queries
        assert await _names(first) == ["asha"]
        assert first.managed_db is not None and first.managed_db.pid is not None
        os.kill(first.managed_db.pid, 0)


# -------------------------------------------------------- backup / restore
async def test_concurrent_backups_while_running_are_all_valid(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as rt:
        await _add_user(rt, "asha")
        outs = [tmp_path / "o" / f"b{i}.dump" for i in range(4)]
        made = await asyncio.gather(
            *(asyncio.to_thread(admin.backup, settings, o) for o in outs)
        )
        assert len(made) == 4
        assert await _names(rt) == ["asha"]  # the live database kept serving
    for out in outs:
        assert out.stat().st_mode & 0o777 == 0o600
        listing = subprocess.run(
            [str(pg_bin / "pg_restore"), "--list", str(out)],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert listing.returncode == 0, listing.stderr
        assert "app_user" in listing.stdout


@pytest.mark.parametrize("kind", ["garbage", "truncated", "empty"])
async def test_a_bad_dump_never_destroys_the_live_database(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path], kind: str
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as rt:
        await _add_user(rt, "asha")
        good = Path((await asyncio.to_thread(admin.backup, settings, None))["path"])
    raw = good.read_bytes()
    bad = tmp_path / f"{kind}.dump"
    bad.write_bytes(
        {
            "garbage": b"this is not a dump\n" * 100,
            "truncated": raw[: len(raw) // 2],
            "empty": b"",
        }[kind]
    )
    with pytest.raises(ManagedDbError) as info:
        await asyncio.to_thread(admin.restore, settings, bad)
    assert info.value.reason is ManagedDbReason.RESTORE_FAILED
    assert not data_dir_holders(root / "data"), "restore left postgres running"
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["asha"], "failed restore lost the live data"
    with pytest.raises(ManagedDbError):
        await asyncio.to_thread(admin.restore, settings, tmp_path / "missing.dump")
    with pytest.raises(ManagedDbError):
        await asyncio.to_thread(admin.restore, settings, tmp_path)  # a directory


async def test_restore_twice_and_restore_is_idempotent(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as rt:
        await _add_user(rt, "asha")
        dump = Path((await asyncio.to_thread(admin.backup, settings, None))["path"])
        await _add_user(rt, "late")
    for _ in range(2):
        await asyncio.to_thread(admin.restore, settings, dump)
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["asha"]
        await _add_user(rt, "after")  # the app role can write after a restore
        assert await _names(rt) == ["after", "asha"]


async def test_dev_compose_dump_imports_and_is_owned_by_the_app_role(
    tmp_path: Path,
    pg_bin: Path,
    db_dirs: tuple[Path, Path],
    pg_settings: DatabaseSettings,
) -> None:
    """Q21: pg_dump of the dev (superuser-owned, external) database restored into
    the managed cluster; every object ends up owned by the non-superuser app role."""
    from meshforge.controller.auth.users import UserStore
    from meshforge.controller.store.engine import create_engine
    from meshforge.controller.store.migrate import upgrade_to_head
    from meshforge.controller.store.models.auth import UserRole

    await asyncio.to_thread(upgrade_to_head, pg_settings)
    engine = create_engine(pg_settings)
    try:
        await UserStore(engine).create_user(
            "devadmin", pw_hash="x" * 20, role=UserRole.ADMIN, actor="dev"
        )
    finally:
        await engine.dispose()
    dump = tmp_path / "dev.dump"
    env = {**os.environ, "PGPASSWORD": pg_settings.password.get_secret_value()}
    subprocess.run(
        [str(pg_bin / "pg_dump"), "-Fc", "-h", pg_settings.host, "-p",
         str(pg_settings.port), "-U", pg_settings.user, "-f", str(dump), pg_settings.name],
        check=True, env=env, capture_output=True, timeout=120,
    )  # fmt: skip

    root, sock = db_dirs
    settings = _settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    await asyncio.to_thread(admin.restore, settings, dump)
    async with ControllerRuntime.start(settings) as rt:
        assert await _names(rt) == ["devadmin"]
        await _add_user(rt, "second")  # app role can write the imported tables
        password = (root / "admin.password").read_text().strip()
        conn = await asyncpg.connect(
            host="127.0.0.1", port=settings.controller.db.port,
            user="meshforge_admin", password=password, database="meshforge",
        )  # fmt: skip
        try:
            owners = {
                r["tableowner"]
                for r in await conn.fetch(
                    "SELECT tableowner FROM pg_tables WHERE schemaname='public'"
                )
            }
            seq_owners = {
                r["sequenceowner"]
                for r in await conn.fetch(
                    "SELECT sequenceowner FROM pg_sequences WHERE schemaname='public'"
                )
            }
        finally:
            await conn.close()
        assert owners == {"meshforge"}, owners
        assert seq_owners <= {"meshforge"}, seq_owners
