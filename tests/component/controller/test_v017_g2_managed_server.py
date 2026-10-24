"""V0.17 G2 (D390, Q18): the real controller runs on its own managed PostgreSQL.

Through :class:`ControllerRuntime` and the real ``meshforge-controller`` process
(not the cluster class alone, which SP2 tested): the bundled, trimmed PostgreSQL
16 is started before the migrations, the app connects as a non-superuser that
owns every table, the postmaster's death stops the controller with FAILURE, a
SIGKILLed controller process takes its postmaster with it (parent-death signal)
and the next start works. No Docker, no mocks.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mf_fabric import seed_ca_dir
from mf_managed_pg import free_port, managed_pg_bin_dir, short_dir
from mf_session_client import poll_until

pytest.importorskip("asyncpg", reason="needs the controller extra")

import asyncpg
from mf_controller_harness import make_settings

from meshforge.common.errors import ExitCode
from meshforge.controller.db_managed.procs import data_dir_holders, pid_alive
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.slow,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
]


@pytest.fixture(scope="session")
def pg_bin() -> Path:
    return managed_pg_bin_dir()


@pytest.fixture
def db_dirs(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """(root, socket dir): root with a space like the team's checkout; the
    socket dir short (107-byte limit)."""
    root = tmp_path / "FINAL YEAR" / "db"
    with short_dir("mfpg-g2-") as sock:
        yield root, sock
    data = root / "data"
    left = data_dir_holders(data) if data.exists() else []
    assert left == [], f"postgres processes left behind: {left}"


def _managed_settings(
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


async def _admin(settings: ControllerSettings, root: Path) -> asyncpg.Connection:
    password = (root / "admin.password").read_text().strip()
    return await asyncpg.connect(
        host="127.0.0.1",
        port=settings.controller.db.port,
        user="meshforge_admin",
        password=password,
        database="meshforge",
        timeout=10,
    )


async def test_runtime_starts_migrates_and_stops_its_own_postgres(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _managed_settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.managed_db is not None
        pid = runtime.managed_db.pid
        assert pid is not None and pid_alive(pid)
        conn = await _admin(settings, root)
        try:
            owners = {
                row["tableowner"]
                for row in await conn.fetch(
                    "SELECT tableowner FROM pg_tables WHERE schemaname = 'public'"
                )
            }
            app = await conn.fetchrow(
                "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                "WHERE rolname = 'meshforge'"
            )
            db_owner = await conn.fetchval(
                "SELECT pg_get_userbyid(datdba) FROM pg_database "
                "WHERE datname = 'meshforge'"
            )
            version_owner = await conn.fetchval(
                "SELECT tableowner FROM pg_tables WHERE tablename = 'alembic_version'"
            )
        finally:
            await conn.close()
        # Q18: every table (alembic's too) is owned by the app role, which is
        # no superuser and cannot create databases or roles.
        assert owners == {"meshforge"}, owners
        assert version_owner == "meshforge"
        assert app is not None and not any(app.values())
        assert db_owner == "meshforge"
        # The engine connects as the app role (a query through the registry).
        assert runtime.registry is not None
        assert await runtime.registry.list_sessions() is not None
        for name, mode in (("admin.password", 0o600), ("app.password", 0o600)):
            assert (root / name).stat().st_mode & 0o777 == mode
    assert not pid_alive(pid)
    assert not (root / "data" / "postmaster.pid").exists()


async def test_app_role_cannot_escalate(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _managed_settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings):
        conn = await asyncpg.connect(
            host="127.0.0.1",
            port=settings.controller.db.port,
            user="meshforge",
            password=(root / "app.password").read_text().strip(),
            database="meshforge",
            timeout=10,
        )
        try:
            for statement in (
                "CREATE DATABASE other",
                "CREATE ROLE intruder",
                "ALTER ROLE meshforge SUPERUSER",
                "COPY (SELECT 1) TO PROGRAM 'true'",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(statement)
        finally:
            await conn.close()
        # Other databases are closed to it (CONNECT revoked from PUBLIC).
        with pytest.raises(asyncpg.InvalidCatalogNameError):
            await asyncpg.connect(
                host="127.0.0.1",
                port=settings.controller.db.port,
                user="meshforge",
                password=(root / "app.password").read_text().strip(),
                database="nonexistent",
                timeout=10,
            )


async def test_postmaster_death_stops_the_controller_with_failure(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
    settings = _managed_settings(tmp_path, pg_bin, root, sock, free_port())
    seed_ca_dir(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.managed_db is not None and runtime.managed_db.pid is not None
        os.kill(runtime.managed_db.pid, signal.SIGKILL)
        await asyncio.wait_for(runtime.wait_for_stop(), 15)
        assert runtime.failed_task == "managed-db"
        assert runtime.exit_code is ExitCode.FAILURE
    # The next start recovers (stale pidfile, crash recovery).
    async with ControllerRuntime.start(settings) as again:
        assert again.managed_db is not None and again.managed_db.pid is not None


def _healthz(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/healthz", timeout=1
        ) as resp:
            return dict(json.loads(resp.read()))
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return None


def _spawn_controller(cfg: Path) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from meshforge.controller.app import main; main()",
            "--config",
            str(cfg),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_healthy(proc: subprocess.Popen[str], port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(f"controller exited {proc.returncode}:\n{out}")
        if _healthz(port) is not None:
            return
        time.sleep(0.1)
    raise AssertionError("controller did not become healthy")


async def test_sigkilled_controller_process_takes_postgres_with_it(
    tmp_path: Path, pg_bin: Path, db_dirs: tuple[Path, Path]
) -> None:
    root, sock = db_dirs
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
        "shutdown_drain_s = 2.0\n"
        "[controller.discovery]\nadvertise = false\n"
        '[controller.enroll]\nbind = "127.0.0.1:0"\n'
        "[controller.db]\n"
        'mode = "managed"\n'
        f"root = {str(root)!r}\n"
        f"socket_dir = {str(sock)!r}\n"
        f"bin_dir = {str(pg_bin)!r}\n"
        f"port = {db_port}\n",
        encoding="utf-8",
    )
    proc = _spawn_controller(cfg)
    try:
        await asyncio.to_thread(_wait_healthy, proc, http_port, 60)
        holders = data_dir_holders(root / "data")
        assert holders, "no postgres process runs in the data dir"
        proc.kill()  # no shutdown code runs at all
        proc.wait(timeout=10)

        # PR_SET_PDEATHSIG=SIGINT: fast shutdown, every backend gone.
        async def postgres_gone() -> bool:
            return not data_dir_holders(root / "data")

        await poll_until(postgres_gone, timeout_s=15, interval_s=0.05)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()

    # A graceful run afterwards: starts, then SIGTERM exits 0 and stops postgres.
    again = _spawn_controller(cfg)
    try:
        await asyncio.to_thread(_wait_healthy, again, http_port, 60)
        again.send_signal(signal.SIGTERM)
        assert again.wait(timeout=60) == 0
        assert data_dir_holders(root / "data") == []
    finally:
        if again.poll() is None:
            again.kill()
            again.wait(timeout=10)
        if again.stdout is not None:
            again.stdout.close()
