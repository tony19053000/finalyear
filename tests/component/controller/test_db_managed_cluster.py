"""Spike SP2 (0.17.0): the managed PostgreSQL prototype against the real bundle.

Downloads the pinned relocatable PostgreSQL 16 build once (cached under
``.cache/meshforge-pg``, or ``$MESHFORGE_PG_CACHE``), trims it exactly like the
package will, and runs every lifecycle scenario on the **trimmed** tree - so a
missing file in the trim rule fails here. ``slow`` (first run downloads ~39 MB);
skipped with the reason when offline, not on Linux x86_64, or glibc < 2.36.
No Docker, no mocks.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

pytest.importorskip("asyncpg", reason="needs the controller extra")

from meshforge.controller.db_managed import (
    LINUX_X86_64,
    ManagedCluster,
    ManagedDbError,
    ManagedDbReason,
    ManagedLayout,
    PgBinaries,
)
from meshforge.controller.db_managed.procs import data_dir_holders

REPO_ROOT = Path(__file__).resolve().parents[3]
LAB_DIR = REPO_ROOT / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._sp2 import scenarios  # noqa: E402
from spikes._sp2.bundle import BundleError, ensure_trimmed  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.asyncio, pytest.mark.timeout(300)]


def _glibc_ok() -> bool:
    name, version = platform.libc_ver()
    if name != "glibc" or not version:
        return False
    need = tuple(int(p) for p in (LINUX_X86_64.min_glibc or "0").split("."))
    return tuple(int(p) for p in version.split(".")[:2]) >= need


@pytest.fixture(scope="session")
def pg_bin() -> Path:
    if not (sys.platform.startswith("linux") and platform.machine() == "x86_64"):
        pytest.skip("the pinned PostgreSQL bundle is Linux x86_64 only")
    if not _glibc_ok():
        pytest.skip(f"the bundle needs glibc >= {LINUX_X86_64.min_glibc}")
    try:
        bundle = ensure_trimmed(LINUX_X86_64)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        pytest.skip(f"cannot download the PostgreSQL bundle (offline?): {exc}")
    except BundleError as exc:  # checksum mismatch etc.: a real failure
        pytest.fail(str(exc))
    return bundle.bin_dir


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    # A space in the path, like the team's "FINAL YEAR" checkout.
    path = tmp_path / "FINAL YEAR" / "db"
    yield path
    leftovers = data_dir_holders(path / "data") if (path / "data").exists() else []
    assert leftovers == [], f"postgres processes left behind: {leftovers}"


async def test_lifecycle_fresh_and_warm(pg_bin: Path, root: Path) -> None:
    result = await scenarios.lifecycle(pg_bin, root)
    assert result["server_version"] == LINUX_X86_64.version
    assert result["first_start"]["initdb_ms"] is not None
    assert result["first_start"]["password_created"]
    assert result["warm_start"]["initdb_ms"] is None
    assert not result["warm_start"]["password_created"]
    assert result["first_stop"] == {**result["first_stop"], "mode": "smart", "exit": 0}
    assert result["warm_stop"]["mode"] == "smart"
    assert result["password_file_mode"] == "0o600"
    assert result["data_dir_mode"] == "0o700"
    assert not result["pidfile_after_stop"]
    if shutil.which("ss"):
        tcp = [entry for entry in result["listeners"] if ".s.PGSQL." not in entry]
        assert tcp and all(entry.split()[-1].startswith("127.0.0.1:") for entry in tcp)
    assert not (root / "data.initdb-tmp").exists()


async def test_refuses_other_major_version(pg_bin: Path, root: Path) -> None:
    result = await scenarios.version_mismatch(pg_bin, root)
    assert result["refused"]
    assert result["error"] == "ClusterVersionMismatchError"
    assert result["failure_type"] == "config_error"
    assert "PostgreSQL 15" in result["message"]


async def test_refuses_non_cluster_data_dir(pg_bin: Path, root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "data" / "precious.txt").write_text("not a database")
    with scenarios.short_socket_dir() as sock:
        cluster = ManagedCluster(
            PgBinaries(pg_bin),
            ManagedLayout(root, socket_dir_override=sock),
            port=scenarios.free_port(),
        )
        with pytest.raises(ManagedDbError) as info:
            await cluster.start()
    assert info.value.reason is ManagedDbReason.DATA_DIR_INVALID
    assert (root / "data" / "precious.txt").read_text() == "not a database"


async def test_interrupted_initdb_leftover_is_replaced(
    pg_bin: Path, root: Path
) -> None:
    (root / "data.initdb-tmp" / "base").mkdir(parents=True)
    (root / "data.initdb-tmp" / "PG_VERSION").write_text("16\n")
    async with scenarios.managed(pg_bin, root) as cluster:
        assert cluster.pid is not None
    assert not (root / "data.initdb-tmp").exists()
    assert (root / "data" / "PG_VERSION").read_text().strip() == "16"


async def test_stale_corrupt_and_reused_pidfiles(pg_bin: Path, root: Path) -> None:
    result = await scenarios.stale_pidfile(pg_bin, root)
    assert result["dead_pid"]["reconcile"] == "stale_pidfile_removed"
    assert result["reused_pid"]["reconcile"] == "stale_pidfile_removed"
    assert result["reused_pid"]["unrelated_process_untouched"]
    assert result["corrupt"]["reconcile"] == "corrupt_pidfile_removed"


async def test_crash_without_death_signal_orphan_is_stopped(
    pg_bin: Path, root: Path
) -> None:
    result = await scenarios.crash(pg_bin, root, die_with_parent=False, watch_s=1.0)
    assert result["postmaster_survived_parent_kill"]  # the measured problem
    assert result["restart_reconcile"] == "orphan_stopped"
    assert result["restart_reconcile_signal"] == "SIGINT"
    assert result["committed_rows_after_restart"] == 1


async def test_crash_with_death_signal_postmaster_exits(
    pg_bin: Path, root: Path
) -> None:
    result = await scenarios.crash(pg_bin, root, die_with_parent=True)
    assert not result["postmaster_survived_parent_kill"]
    assert result["postmaster_exit_after_ms"] < 3000
    assert result["processes_in_data_dir_after_watch"] == 0
    assert result["restart_reconcile"] == "clean"
    assert result["committed_rows_after_restart"] == 1
    assert any("fast shutdown" in line for line in result["server_log_shutdown_lines"])


async def test_port_in_use_is_typed_and_leaves_nothing(
    pg_bin: Path, root: Path
) -> None:
    result = await scenarios.port_in_use(pg_bin, root)
    assert result["reason"] == "port_in_use"
    assert result["processes_left"] == 0
    assert result["lock_released"]


async def test_second_manager_is_refused(pg_bin: Path, root: Path) -> None:
    assert await scenarios.concurrent_start(pg_bin, root) == {"reason": "locked"}


async def test_postmaster_sigkill_recovers(pg_bin: Path, root: Path) -> None:
    result = await scenarios.postmaster_killed(pg_bin, root)
    assert result["stop_after_kill"] == "already_exited"
    assert result["exit_code"] == -9
    assert result["committed_rows_after_recovery"] == 1
    assert any("automatic recovery" in line for line in result["server_log"])


async def test_migrations_store_smoke_and_dump_restore(
    pg_bin: Path, root: Path
) -> None:
    from meshforge.controller.store.migrate import head_revision

    async with scenarios.managed(pg_bin, root) as cluster:
        smoke = await scenarios.migrate_and_smoke(cluster)
        backup = await scenarios.dump_restore(cluster, cluster.layout.backups_dir)
    assert smoke["target_revision"] == head_revision()
    assert smoke["revision_before"] is None
    assert smoke["job_created"] and smoke["job_state"] == "queued"
    assert backup["identical_row_counts"]
    assert backup["tables"] > 10 and backup["rows_total"] > 0
    assert backup["dump_file_mode"] == "0o600"
    assert not list(cluster.layout.socket_dir.glob(".pgpass*"))


async def test_password_never_logged_or_on_a_command_line(
    pg_bin: Path, root: Path
) -> None:
    with structlog.testing.capture_logs() as logs:
        async with scenarios.managed(pg_bin, root) as cluster:
            password = cluster.database_settings().password.get_secret_value()
            pid = cluster.pid
            assert pid is not None
            cmdline = _read(Path(f"/proc/{pid}/cmdline"))
            environ = _read(Path(f"/proc/{pid}/environ"))
            await cluster.dump(cluster.layout.backups_dir / "x.dump")
    assert password.encode() not in cmdline
    assert password.encode() not in environ
    assert password not in json.dumps(logs, default=str)
    for path in (root / "log").iterdir():
        assert password not in path.read_text(errors="replace"), path
    for name in ("admin.password", "app.password"):  # Q18 role split (V0.17 G2)
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600, name
        assert os.stat(root / name).st_uid == os.geteuid(), name


def _read(path: Path) -> bytes:
    return path.read_bytes()
