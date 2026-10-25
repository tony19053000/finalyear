"""Backups of the managed database (V0.17 G2, D393 controller half).

**Pre-upgrade backup.** Before the migrations of a start whose controller version
differs from the one that last started successfully on this cluster - or whose
database is at another migration revision than this version's head - the
controller dumps the database to ``<root>/backups/pre-<version>.dump`` and keeps
the newest :attr:`DbSettings.backups_keep` (3) of those. The version that last
started successfully is ``<root>/schema-version.json`` (written only after the
start's health check passed). If the start then fails, or is not serving within
``health_timeout_s``, the backup is restored and the controller exits non-zero
(``config_error``: systemd does not restart it; G5's upgrade switches the
program pointer back and starts the previous version on the restored data).

**Manual backups** (``meshforge-controller db backup``) default to
``<root>/backups/manual-<UTC time>.dump`` and are never deleted automatically.

**Rollback that failed.** If restoring the pre-upgrade backup itself fails, the
controller writes ``<root>/rollback-failed.json`` (which backup, why, when):
``db status`` reports it and the package's postinst says the database was *not*
restored. A successful ``db restore`` or a good start removes it.

A fresh (empty) database is never backed up. The dump files are owner-only
(:meth:`ManagedCluster.dump`). External mode (``[controller.db] mode =
"external"``) has no automatic backups: whoever runs that PostgreSQL owns them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.logging import get_logger
from meshforge.controller.db_managed.cluster import ManagedCluster

__all__ = [
    "MANUAL_PREFIX",
    "MARKER_NAME",
    "PRE_UPGRADE_PREFIX",
    "ROLLBACK_FAILED_NAME",
    "BackupFile",
    "SchemaMarker",
    "UpgradeBackup",
    "clear_marker",
    "clear_rollback_failed",
    "list_backups",
    "manual_backup_path",
    "needs_upgrade_backup",
    "pre_upgrade_path",
    "read_marker",
    "read_rollback_failed",
    "rotate",
    "take_upgrade_backup",
    "write_marker",
    "write_rollback_failed",
]

log = get_logger(__name__)

PRE_UPGRADE_PREFIX: Final = "pre-"
MANUAL_PREFIX: Final = "manual-"
DUMP_SUFFIX: Final = ".dump"
MARKER_NAME: Final = "schema-version.json"
ROLLBACK_FAILED_NAME: Final = "rollback-failed.json"
_SAFE: Final = re.compile(r"[^A-Za-z0-9._+-]")
_MAX_MARKER_BYTES: Final = 4096


def _safe(text: str) -> str:
    """A version string as a file-name part (PEP 440 versions are already safe;
    anything else is replaced, never trusted as a path)."""
    cleaned = _SAFE.sub("_", text).strip(".")
    return cleaned[:100] or "unknown"


def pre_upgrade_path(backups_dir: Path, version: str) -> Path:
    """``pre-<version>.dump``: the state *before* ``version`` migrated it."""
    return backups_dir / f"{PRE_UPGRADE_PREFIX}{_safe(version)}{DUMP_SUFFIX}"


def manual_backup_path(backups_dir: Path, now: dt.datetime | None = None) -> Path:
    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%dT%H%M%SZ")
    return backups_dir / f"{MANUAL_PREFIX}{stamp}{DUMP_SUFFIX}"


@dataclass(frozen=True, slots=True)
class BackupFile:
    path: Path
    kind: str
    """``pre-upgrade`` / ``manual`` / ``other``."""
    size_bytes: int
    modified: dt.datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "modified": self.modified.isoformat(),
        }


def list_backups(backups_dir: Path) -> list[BackupFile]:
    """Every ``*.dump`` in ``backups_dir``, oldest first (partial files and
    anything else are ignored)."""
    found: list[BackupFile] = []
    try:
        entries = list(backups_dir.iterdir())
    except FileNotFoundError:
        return []
    for path in entries:
        if not path.name.endswith(DUMP_SUFFIX) or path.name.startswith("."):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        if path.name.startswith(PRE_UPGRADE_PREFIX):
            kind = "pre-upgrade"
        elif path.name.startswith(MANUAL_PREFIX):
            kind = "manual"
        else:
            kind = "other"
        found.append(
            BackupFile(
                path,
                kind,
                st.st_size,
                dt.datetime.fromtimestamp(st.st_mtime, dt.UTC),
            )
        )
    found.sort(key=lambda b: (b.modified, b.path.name))
    return found


def rotate(backups_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` pre-upgrade backups; returns the
    deleted paths. Manual backups are never touched."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    pre = [b for b in list_backups(backups_dir) if b.kind == "pre-upgrade"]
    removed: list[Path] = []
    for backup in pre[:-keep]:
        with contextlib.suppress(FileNotFoundError):
            backup.path.unlink()
            removed.append(backup.path)
    if removed:
        log.info(
            "controller.db.backups_rotated",
            removed=[p.name for p in removed],
            kept=keep,
        )
    return removed


# ------------------------------------------------------------------ marker
@dataclass(frozen=True, slots=True)
class SchemaMarker:
    """What last started successfully on this cluster."""

    version: str
    revision: str | None
    at: str


def read_marker(root: Path) -> SchemaMarker | None:
    """``<root>/schema-version.json``, or None (missing or unreadable - a
    damaged marker only means "back up to be safe")."""
    path = root / MARKER_NAME
    try:
        raw = path.read_bytes()[:_MAX_MARKER_BYTES]
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        return None
    revision = data.get("revision")
    return SchemaMarker(
        version=data["version"],
        revision=revision if isinstance(revision, str) else None,
        at=str(data.get("at", "")),
    )


def write_marker(root: Path, *, version: str, revision: str | None) -> None:
    body = {
        "version": version,
        "revision": revision,
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }
    atomic_write_bytes(
        root / MARKER_NAME,
        (json.dumps(body, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )


def clear_marker(root: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (root / MARKER_NAME).unlink()


# ------------------------------------------------------ failed-rollback marker
def write_rollback_failed(
    root: Path,
    *,
    backup: Path,
    from_version: str | None,
    to_version: str,
    error: str,
) -> None:
    """Record that the D393 rollback did not restore ``backup`` (persistent:
    survives restarts until a restore or a good start clears it)."""
    body = {
        "backup": str(backup),
        "from_version": from_version,
        "to_version": to_version,
        "error": error[:500],
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }
    atomic_write_bytes(
        root / ROLLBACK_FAILED_NAME,
        (json.dumps(body, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )


def read_rollback_failed(root: Path) -> dict[str, object] | None:
    """The failed-rollback record, or None. A file that exists but cannot be
    parsed still counts (the rollback failed; only its details are lost)."""
    path = root / ROLLBACK_FAILED_NAME
    try:
        raw = path.read_bytes()[:_MAX_MARKER_BYTES]
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"error": f"unreadable record: {exc.strerror or exc}"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"error": "damaged record"}
    if not isinstance(data, dict):
        return {"error": "damaged record"}
    return {str(k): v for k, v in data.items()}


def clear_rollback_failed(root: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (root / ROLLBACK_FAILED_NAME).unlink()


def needs_upgrade_backup(
    marker: SchemaMarker | None,
    *,
    version: str,
    db_revision: str | None,
    head_revision: str,
) -> bool:
    """Back up before migrating? (module docstring). Pure.

    Never for an empty database (``db_revision`` None). Otherwise when the
    version changed since the last good start, when there is no (readable)
    marker, or when the database is not at this version's head revision.
    """
    if db_revision is None:
        return False
    if marker is None or marker.version != version:
        return True
    return db_revision != head_revision


@dataclass(frozen=True, slots=True)
class UpgradeBackup:
    """A pre-upgrade backup taken by this start."""

    path: Path
    from_version: str | None
    from_revision: str | None
    to_version: str
    dump_s: float


async def take_upgrade_backup(
    cluster: ManagedCluster,
    *,
    version: str,
    db_revision: str | None,
    head_revision: str,
    keep: int,
) -> UpgradeBackup | None:
    """Dump before migrating when :func:`needs_upgrade_backup` says so, then
    rotate; None when no backup was needed. A failed dump raises
    ``dump_failed`` - the controller then refuses to migrate (better down than
    migrated without a way back)."""
    root = cluster.layout.root
    marker = read_marker(root)
    if not needs_upgrade_backup(
        marker, version=version, db_revision=db_revision, head_revision=head_revision
    ):
        return None
    backups = cluster.layout.backups_dir
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = pre_upgrade_path(backups, version)
    seconds = await cluster.dump(target)
    if os.name == "posix":
        os.chmod(target, 0o600)
    rotate(backups, keep)
    backup = UpgradeBackup(
        path=target,
        from_version=None if marker is None else marker.version,
        from_revision=db_revision,
        to_version=version,
        dump_s=seconds,
    )
    log.warning(
        "controller.db.upgrade_backup",
        path=str(target),
        from_version=backup.from_version,
        from_revision=db_revision,
        to_version=version,
        to_revision=head_revision,
        dump_ms=round(seconds * 1000),
    )
    return backup
