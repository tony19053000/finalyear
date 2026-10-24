"""Spike SP2 measured scenarios (used by ``sp2_managed_pg.py`` and the component tests).

Every scenario takes a scratch directory, builds its own cluster there, and returns
a JSON-safe dict of what it measured. Each cleans up (stops what it started) even
when it fails. None needs Docker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final

from meshforge.controller.db_managed import (
    ClusterVersionMismatchError,
    ManagedCluster,
    ManagedLayout,
    PgBinaries,
)
from meshforge.controller.db_managed.pidfile import read_postmaster_pid
from meshforge.controller.db_managed.procs import data_dir_holders, pid_alive

__all__ = [
    "concurrent_start",
    "crash",
    "dump_restore",
    "free_port",
    "lifecycle",
    "managed",
    "migrate_and_smoke",
    "port_in_use",
    "postmaster_killed",
    "short_socket_dir",
    "stale_pidfile",
    "version_mismatch",
]

CRASH_CHILD: Final = Path(__file__).resolve().parent / "crash_child.py"
_LOG_KEYS: Final = (
    "received",
    "was shut down",
    "was interrupted",
    "not properly shut down",
    "redo ",
    "terminated by signal",
    "is ready",
)


def free_port() -> int:
    """A currently free loopback port (race-prone by nature; fine for a spike)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def short_socket_dir() -> Iterator[Path]:
    """A short socket dir under the system temp dir (sun_path limit, 107 bytes)."""
    path = Path(tempfile.mkdtemp(prefix="mfpg-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _cluster(bin_dir: Path, root: Path, socket_dir: Path, port: int) -> ManagedCluster:
    return ManagedCluster(
        PgBinaries(bin_dir),
        ManagedLayout(root, socket_dir_override=socket_dir),
        port=port,
    )


@contextlib.asynccontextmanager
async def managed(bin_dir: Path, root: Path) -> AsyncIterator[ManagedCluster]:
    """A started cluster under ``root``; stopped on exit."""
    with short_socket_dir() as sock:
        cluster = _cluster(bin_dir, root, sock, free_port())
        await cluster.start()
        try:
            yield cluster
        finally:
            await cluster.stop()


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


async def lifecycle(bin_dir: Path, root: Path) -> dict[str, Any]:
    """Fresh start (initdb), stop, warm restart, stop; secret file mode; listeners."""
    with short_socket_dir() as sock:
        port = free_port()
        cluster = _cluster(bin_dir, root, sock, port)
        first = await cluster.start()
        listeners = _listeners(port)
        stop1 = await cluster.stop()
        second = await cluster.start()
        stop2 = await cluster.stop()
    pw = cluster.layout.password_file.stat()
    return {
        "server_version": first.server_version,
        "first_start": {
            "initdb_ms": _ms(first.initdb_s),
            "ready_ms": _ms(first.ready_s),
            "total_ms": _ms(first.total_s),
            "password_created": first.password_created,
        },
        "first_stop": {
            "mode": stop1.mode,
            "ms": _ms(stop1.duration_s),
            "exit": stop1.exit_code,
        },
        "warm_start": {
            "initdb_ms": _ms(second.initdb_s),
            "ready_ms": _ms(second.ready_s),
            "total_ms": _ms(second.total_s),
            "password_created": second.password_created,
        },
        "warm_stop": {
            "mode": stop2.mode,
            "ms": _ms(stop2.duration_s),
            "exit": stop2.exit_code,
        },
        "password_file_mode": oct(pw.st_mode & 0o777),
        "data_dir_mode": oct(cluster.layout.data_dir.stat().st_mode & 0o777),
        "listeners": listeners,
        "pidfile_after_stop": (cluster.layout.data_dir / "postmaster.pid").exists(),
    }


def _listeners(port: int) -> list[str]:
    """``ss`` lines for the port (proves loopback-only binding)."""
    ss = shutil.which("ss")
    if ss is None:
        return ["ss not installed"]
    done = subprocess.run(
        [ss, "-H", "-ltnx"], capture_output=True, text=True, check=False
    )
    return sorted(
        " ".join(line.split()[3:5])
        for line in done.stdout.splitlines()
        if f":{port}" in line or f".s.PGSQL.{port}" in line
    )


async def version_mismatch(bin_dir: Path, root: Path) -> dict[str, Any]:
    """Pretend the data dir is from PostgreSQL 15 and try to start."""
    async with managed(bin_dir, root):
        pass
    (root / "data" / "PG_VERSION").write_text("15\n")
    try:
        with short_socket_dir() as sock:
            await _cluster(bin_dir, root, sock, free_port()).start()
    except ClusterVersionMismatchError as exc:
        return {
            "refused": True,
            "error": type(exc).__name__,
            "failure_type": exc.failure_type.value,
            "message": str(exc),
        }
    finally:
        (root / "data" / "PG_VERSION").write_text("16\n")
    return {"refused": False}


async def stale_pidfile(bin_dir: Path, root: Path) -> dict[str, Any]:
    """A ``postmaster.pid`` naming (a) a dead pid, (b) a live unrelated process."""
    async with managed(bin_dir, root) as cluster:
        good = (cluster.layout.data_dir / "postmaster.pid").read_text()
    lines = good.split("\n")
    results: dict[str, Any] = {}
    # (a) dead pid: spawn and reap a process, reuse its pid in the file.
    dead = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await dead.wait()
    # (b) live unrelated process: PostgreSQL alone would refuse to start.
    live = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(120)"
    )
    try:
        for case, pid in (("dead_pid", dead.pid), ("reused_pid", live.pid)):
            lines[0] = str(pid)
            (root / "data" / "postmaster.pid").write_text("\n".join(lines))
            with short_socket_dir() as sock:
                cluster = _cluster(bin_dir, root, sock, free_port())
                started = time.monotonic()
                outcome = await cluster.start()
                results[case] = {
                    "reconcile": outcome.reconcile.kind.value,
                    "start_ms": _ms(time.monotonic() - started),
                    "unrelated_process_untouched": pid_alive(live.pid),
                }
                await cluster.stop()
        (root / "data" / "postmaster.pid").write_text("garbage\n")
        with short_socket_dir() as sock:
            cluster = _cluster(bin_dir, root, sock, free_port())
            outcome = await cluster.start()
            results["corrupt"] = {"reconcile": outcome.reconcile.kind.value}
            await cluster.stop()
    finally:
        live.kill()
        await live.wait()
    return results


async def crash(
    bin_dir: Path, root: Path, *, die_with_parent: bool, watch_s: float = 3.0
) -> dict[str, Any]:
    """SIGKILL a "controller" process; does its postmaster survive? Then restart."""
    with short_socket_dir() as sock:
        port = free_port()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CRASH_CHILD),
            str(bin_dir),
            str(root),
            str(sock),
            str(port),
            "1" if die_with_parent else "0",
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            info = await asyncio.wait_for(_ready_line(proc), 120)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        postmaster = int(info["postmaster_pid"])
        os.kill(proc.pid, signal.SIGKILL)
        await proc.wait()
        killed_at = time.monotonic()
        gone_after: float | None = None
        while time.monotonic() - killed_at < watch_s:
            if not pid_alive(postmaster):
                gone_after = time.monotonic() - killed_at
                break
            await asyncio.sleep(0.02)
        survived = pid_alive(postmaster)
        holders = data_dir_holders(root / "data")
        pidfile = read_postmaster_pid(root / "data")
        cluster = _cluster(bin_dir, root, sock, port)
        restart = await cluster.start()
        try:
            rows = await _probe_rows(cluster)
        finally:
            await cluster.stop()
        log_tail = _server_log_tail(root / "log")
    return {
        "die_with_parent": die_with_parent,
        "postmaster_survived_parent_kill": survived,
        "postmaster_exit_after_ms": _ms(gone_after),
        "processes_in_data_dir_after_watch": len(holders),
        "pidfile_left": pidfile is not None,
        "restart_reconcile": restart.reconcile.kind.value,
        "restart_reconcile_signal": restart.reconcile.signal,
        "restart_reconcile_ms": _ms(restart.reconcile.duration_s),
        "restart_total_ms": _ms(restart.total_s),
        "committed_rows_after_restart": rows,
        "server_log_shutdown_lines": log_tail,
    }


async def _ready_line(proc: asyncio.subprocess.Process) -> dict[str, Any]:
    """The child's JSON ready line (its log lines share stdout and are skipped)."""
    if proc.stdout is None:
        raise RuntimeError("crash child has no stdout pipe")
    async for raw in proc.stdout:
        line = raw.decode(errors="replace")
        if line.startswith('{"postmaster_pid"'):
            info: dict[str, Any] = json.loads(line)
            return info
    raise RuntimeError(f"crash child exited ({await proc.wait()}) before it was ready")


def _server_log_tail(log_dir: Path) -> list[str]:
    lines: list[str] = []
    for path in sorted(log_dir.glob("postgresql-*.log")):
        lines += [
            line.split("] ", 1)[-1]
            for line in path.read_text(errors="replace").splitlines()
            if any(key in line for key in _LOG_KEYS)
        ]
    return lines[-12:]


async def _probe_rows(cluster: ManagedCluster) -> int:
    import asyncpg

    s = cluster.database_settings()
    conn = await asyncpg.connect(
        host=s.host,
        port=s.port,
        user=s.user,
        password=s.password.get_secret_value(),
        database=s.name,
    )
    try:
        return int(await conn.fetchval("SELECT count(*) FROM crash_probe"))
    finally:
        await conn.close()


async def migrate_and_smoke(cluster: ManagedCluster) -> dict[str, Any]:
    """``alembic upgrade head`` + node/job store round trip on the managed DB."""
    from meshforge.common.ids import new_job_id, new_node_id, new_session_id
    from meshforge.common.states import JobState
    from meshforge.controller.jobs.store import JobStore
    from meshforge.controller.jobs.transitions import JobEvent
    from meshforge.controller.liveness import NodeState
    from meshforge.controller.registry import Registry
    from meshforge.controller.store.engine import check_connection, create_engine
    from meshforge.controller.store.migrate import upgrade_to_head

    settings = cluster.database_settings()
    started = time.monotonic()
    outcome = await asyncio.to_thread(upgrade_to_head, settings)
    migrate_s = time.monotonic() - started
    again = time.monotonic()
    await asyncio.to_thread(upgrade_to_head, settings)
    noop_s = time.monotonic() - again
    engine = create_engine(settings)
    try:
        await check_connection(engine, settings)
        node = str(new_node_id())
        await Registry(engine).upsert_node_on_hello(
            node_id=node,
            hostname="sp2",
            os="linux",
            platform="x86_64",
            hardware={},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
        )
        store = JobStore(engine)
        record, created = await store.create_job(
            job_id=str(new_job_id()),
            name="sp2-smoke",
            manifest={"name": "sp2-smoke"},
            bundle_digest="sha256:" + "0" * 64,
            placement={"mode": "offload", "node_id": node},
            request_hash="h" * 64,
            idempotency_key=None,
        )
        await store.transition_job(
            record.id, JobEvent.PLAN, expected=JobState.SUBMITTED
        )
        final = await store.transition_job(
            record.id, JobEvent.PLANNED, expected=JobState.PLANNING
        )
    finally:
        await engine.dispose()
    return {
        "target_revision": outcome.target_revision,
        "revision_before": outcome.current_revision,
        "migrate_ms": _ms(migrate_s),
        "noop_migrate_ms": _ms(noop_s),
        "job_created": created,
        "job_state": final.state.value,
        "job_id": record.id,
    }


async def dump_restore(
    cluster: ManagedCluster, backups: Path, *, bulk_rows: int = 0
) -> dict[str, Any]:
    """``pg_dump`` the app DB, restore into a new DB, compare table row counts.

    ``bulk_rows`` > 0 first adds a ``sp2_bulk`` table of that many ~200-byte rows
    (a fabric with a long job history) to time the backup on a bigger database.
    """
    import asyncpg

    s = cluster.database_settings()
    if bulk_rows:
        conn = await asyncpg.connect(
            host=s.host,
            port=s.port,
            user=s.user,
            password=s.password.get_secret_value(),
            database=s.name,
        )
        try:
            await conn.execute(
                "CREATE TABLE sp2_bulk AS SELECT g AS id, md5(g::text) AS a, "
                "repeat(md5((g * 7)::text), 5) AS b, now() AS at "
                "FROM generate_series(1, $1::int) AS g",
                bulk_rows,
            )
        finally:
            await conn.close()
    dump_file = backups / f"pre-sp2-{bulk_rows}.dump"
    dump_s = await cluster.dump(dump_file)
    target = "meshforge_restore"
    await cluster.drop_database(target)
    await cluster.create_database(target)
    restore_s = await cluster.restore(dump_file, database=target)

    async def counts(database: str) -> dict[str, int]:
        conn = await asyncpg.connect(
            host=s.host,
            port=s.port,
            user=s.user,
            password=s.password.get_secret_value(),
            database=database,
        )
        try:
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "ORDER BY tablename"
            )
            result: dict[str, int] = {}
            for row in tables:
                name = row["tablename"]
                result[name] = int(
                    await conn.fetchval(
                        f'SELECT count(*) FROM public."{name}"'  # noqa: S608 - names from pg_tables
                    )
                )
            return result
        finally:
            await conn.close()

    source, restored = await counts(cluster.database), await counts(target)
    db_bytes = await _db_size(cluster, cluster.database)
    await cluster.drop_database(target)
    return {
        "dump_ms": _ms(dump_s),
        "restore_ms": _ms(restore_s),
        "dump_bytes": dump_file.stat().st_size,
        "dump_file_mode": oct(dump_file.stat().st_mode & 0o777),
        "source_db_bytes": db_bytes,
        "tables": len(source),
        "rows_total": sum(source.values()),
        "identical_row_counts": source == restored,
    }


async def _db_size(cluster: ManagedCluster, database: str) -> int:
    import asyncpg

    s = cluster.database_settings()
    conn = await asyncpg.connect(
        host=s.host,
        port=s.port,
        user=s.user,
        password=s.password.get_secret_value(),
        database=database,
    )
    try:
        return int(await conn.fetchval("SELECT pg_database_size(current_database())"))
    finally:
        await conn.close()


async def port_in_use(bin_dir: Path, root: Path) -> dict[str, Any]:
    """Another program listens on the port: typed ``port_in_use``, nothing left."""
    from meshforge.controller.db_managed import ManagedDbError

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = int(blocker.getsockname()[1])
        with short_socket_dir() as sock:
            cluster = _cluster(bin_dir, root, sock, port)
            try:
                await cluster.start()
            except ManagedDbError as exc:
                result: dict[str, Any] = {
                    "reason": exc.reason.value,
                    "message": str(exc),
                }
            else:
                await cluster.stop()
                result = {"reason": None}
    result["processes_left"] = len(data_dir_holders(root / "data"))
    result["lock_released"] = _lock_free(root)
    return result


def _lock_free(root: Path) -> bool:
    from meshforge.common.filelock import try_lock, unlock

    with (root / "managed.lock").open("a+b") as fh:
        if try_lock(fh):
            unlock(fh)
            return True
        return False


async def concurrent_start(bin_dir: Path, root: Path) -> dict[str, Any]:
    """A second manager of the same root is refused (``locked``)."""
    from meshforge.controller.db_managed import ManagedDbError

    async with managed(bin_dir, root):
        with short_socket_dir() as sock:
            try:
                await _cluster(bin_dir, root, sock, free_port()).start()
            except ManagedDbError as exc:
                return {"reason": exc.reason.value}
    return {"reason": None}


async def postmaster_killed(bin_dir: Path, root: Path) -> dict[str, Any]:
    """SIGKILL the postmaster itself (controller alive, a client connected).

    What G2's supervision must handle: the controller sees its child exit; the
    next start finds orphaned backends or not, and PostgreSQL runs crash recovery.
    """
    import asyncpg

    with short_socket_dir() as sock:
        port = free_port()
        cluster = _cluster(bin_dir, root, sock, port)
        await cluster.start()
        s = cluster.database_settings()
        conn = await asyncpg.connect(
            host=s.host,
            port=s.port,
            user=s.user,
            password=s.password.get_secret_value(),
            database=s.name,
        )
        await conn.execute("CREATE TABLE kill_probe (id int)")
        await conn.execute("INSERT INTO kill_probe VALUES (1)")
        pid = cluster.pid
        if pid is None:
            raise RuntimeError("cluster not running")
        os.kill(pid, signal.SIGKILL)
        killed = time.monotonic()
        stop = await cluster.stop()
        strays_after_stop = len(data_dir_holders(root / "data"))
        with contextlib.suppress(Exception):
            await conn.close(timeout=2)
        again = _cluster(bin_dir, root, sock, port)
        restart = await again.start()
        restarted_after = time.monotonic() - killed
        conn2 = await asyncpg.connect(
            host=s.host,
            port=s.port,
            user=s.user,
            password=s.password.get_secret_value(),
            database=s.name,
        )
        rows = int(await conn2.fetchval("SELECT count(*) FROM kill_probe"))
        await conn2.close()
        await again.stop()
    return {
        "stop_after_kill": stop.mode,
        "exit_code": stop.exit_code,
        "backends_left_after_postmaster_kill": strays_after_stop,
        "restart_reconcile": restart.reconcile.kind.value,
        "restart_ready_ms": _ms(restart.ready_s),
        "kill_to_ready_ms": _ms(restarted_after),
        "committed_rows_after_recovery": rows,
        "server_log": _server_log_tail(root / "log"),
    }
