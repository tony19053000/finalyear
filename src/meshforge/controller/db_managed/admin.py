"""``meshforge-controller db backup|restore|status`` (V0.17 G2, D393).

Admin operations on the managed cluster, for a person at the controller host
(the CLI re-executes as the service user, :mod:`meshforge.common.system_cli`):

``backup``
    while the controller runs: ``pg_dump`` of its running cluster (attach, no
    interruption); while it is stopped: start the cluster briefly, dump, stop.
``restore``
    only while the controller is stopped (it would otherwise lose its database
    under it): start the cluster, replace the database with the dump (one
    transaction; objects owned by the application role, so a dump of the dev
    Compose database imports too - Q21), forget the schema marker (the next
    start backs up again before migrating an older dump), stop.
``status``
    mode, paths, port, running or not, data major, the last good start, the
    backups and the free disk - read-only, works whether or not it runs.

External mode (``[controller.db] mode = "external"``) refuses backup/restore:
whoever runs that PostgreSQL owns its backups.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshforge.common.errors import ConfigError
from meshforge.controller.db_managed.backup import (
    clear_marker,
    list_backups,
    manual_backup_path,
    read_marker,
)
from meshforge.controller.db_managed.binaries import BUNDLED_MAJOR
from meshforge.controller.db_managed.cluster import ManagedCluster
from meshforge.controller.db_managed.errors import ManagedDbError, ManagedDbReason
from meshforge.controller.db_managed.pidfile import read_cluster_major
from meshforge.controller.db_managed.runtime import cluster_for

if TYPE_CHECKING:
    from meshforge.controller.settings import ControllerSettings

__all__ = ["backup", "restore", "status"]

_STOP_HINT = "sudo systemctl stop meshforge-controller"


def _require_managed(settings: ControllerSettings, what: str) -> None:
    if not settings.managed_db:
        raise ConfigError(
            f"`db {what}` works on the managed database only; this controller "
            "uses [controller.db] mode = 'external' (use that server's own "
            "pg_dump/pg_restore)",
            details={"mode": settings.controller.db.mode.value},
        )


def backup(settings: ControllerSettings, out: Path | None) -> dict[str, Any]:
    """Dump the database (module docstring); returns facts for the CLI.
    Blocking: runs its own event loop (the cluster's parent-death signal
    follows the calling thread)."""
    _require_managed(settings, "backup")
    cluster = cluster_for(settings)
    target = out or manual_backup_path(cluster.layout.backups_dir)
    target = target.expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if cluster.lock_free():
        seconds = asyncio.run(_dump_started(cluster, target))
        how = "started briefly"
    else:
        cluster.attach()
        seconds = asyncio.run(cluster.dump(target))
        how = "running controller"
    return {
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "seconds": round(seconds, 3),
        "cluster": how,
    }


async def _dump_started(cluster: ManagedCluster, target: Path) -> float:
    await cluster.start()
    try:
        return await cluster.dump(target)
    finally:
        await cluster.stop()


def restore(settings: ControllerSettings, dump: Path) -> dict[str, Any]:
    """Replace the database with ``dump`` (controller stopped only). Blocking."""
    _require_managed(settings, "restore")
    dump = dump.expanduser().resolve()
    if not dump.is_file():
        raise ManagedDbError(
            f"no backup file at {dump}",
            reason=ManagedDbReason.RESTORE_FAILED,
            details={"path": str(dump)},
        )
    cluster = cluster_for(settings)
    if not cluster.lock_free():
        raise ManagedDbError(
            "the controller is running and uses this database; stop it first "
            f"({_STOP_HINT}), restore, then start it again",
            reason=ManagedDbReason.IN_USE,
            details={"root": str(cluster.layout.root)},
        )
    seconds = asyncio.run(_restore_started(cluster, dump))
    clear_marker(cluster.layout.root)
    return {"restored_from": str(dump), "seconds": round(seconds, 3)}


async def _restore_started(cluster: ManagedCluster, dump: Path) -> float:
    await cluster.start()
    try:
        return await cluster.restore_database(dump)
    finally:
        await cluster.stop()


def _running(cluster: ManagedCluster) -> bool:
    if not cluster.layout.data_dir.exists():
        return False
    return not cluster.lock_free()


def status(settings: ControllerSettings) -> dict[str, Any]:
    """Read-only facts about the controller's database."""
    db = settings.controller.db
    if not settings.managed_db:
        return {
            "mode": db.mode.value,
            "url": settings.database.display_url(),
        }
    cluster = cluster_for(settings)
    layout = cluster.layout
    marker = read_marker(layout.root)
    try:
        disk_free: int | None = shutil.disk_usage(
            layout.root if layout.root.exists() else layout.root.parent
        ).free
    except OSError:
        disk_free = None
    return {
        "mode": db.mode.value,
        "root": str(layout.root),
        "data_dir": str(layout.data_dir),
        "socket_dir": str(layout.socket_dir),
        "port": cluster.port,
        "bin_dir": str(cluster.binaries.bin_dir),
        "initialised": layout.data_dir.exists(),
        "data_major": read_cluster_major(layout.data_dir)
        if layout.data_dir.exists()
        else None,
        "bundled_major": BUNDLED_MAJOR,
        "running": _running(cluster),
        "last_good_start": None
        if marker is None
        else {"version": marker.version, "revision": marker.revision, "at": marker.at},
        "backups": [b.as_dict() for b in list_backups(layout.backups_dir)],
        "backups_keep": db.backups_keep,
        "disk_free_bytes": disk_free,
    }
