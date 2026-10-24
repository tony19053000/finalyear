"""Typed errors of the managed PostgreSQL cluster (spike SP2, D390).

One class with a :class:`ManagedDbReason` (like
:class:`~meshforge.common.private_files.PrivateFileError`) so callers and the
future ``meshforge-controller doctor`` can branch on *why*, plus a subclass for the
major-version mismatch the brief asks to be a "clear typed error". Messages and
``details`` never contain the database password.
"""

from __future__ import annotations

from enum import StrEnum

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = ["ClusterVersionMismatchError", "ManagedDbError", "ManagedDbReason"]


class ManagedDbReason(StrEnum):
    """Which step of the managed-cluster lifecycle failed."""

    BINARIES_MISSING = "binaries_missing"
    """A required program (``postgres``, ``initdb``, ``pg_dump``...) is not in the bundle."""
    BINARIES_UNUSABLE = "binaries_unusable"
    """``postgres --version`` failed or printed something unexpected (wrong libc,
    damaged bundle, wrong major)."""
    VERSION_MISMATCH = "version_mismatch"
    """``PG_VERSION`` of the data dir is not the bundle's major version."""
    DATA_DIR_INVALID = "data_dir_invalid"
    """The data dir exists and is not empty, but is not a PostgreSQL cluster."""
    PATH_INVALID = "path_invalid"
    """A path PostgreSQL cannot use (socket path too long, a comma or quote in a
    socket directory)."""
    LOCKED = "locked"
    """Another process (a second controller) holds the managed root's lock."""
    INITDB_FAILED = "initdb_failed"
    START_FAILED = "start_failed"
    """The postmaster exited during start-up (see ``details["log_tail"]``)."""
    PORT_IN_USE = "port_in_use"
    START_TIMEOUT = "start_timeout"
    STOP_FAILED = "stop_failed"
    """The postmaster did not exit even after SIGKILL (should never happen)."""
    ORPHAN_STOP_FAILED = "orphan_stop_failed"
    """A postmaster left by a crashed controller could not be stopped."""
    NOT_RUNNING = "not_running"
    DUMP_FAILED = "dump_failed"
    RESTORE_FAILED = "restore_failed"
    UPGRADE_ROLLED_BACK = "upgrade_rolled_back"
    """D393: the start after an upgrade failed (migrations or the health check)
    and the pre-upgrade backup is restored; the controller exits 4 so systemd
    does not restart it into the same failure."""
    IN_USE = "in_use"
    """``db restore`` while the controller service runs the cluster."""


class ManagedDbError(MeshForgeError):
    """The managed PostgreSQL cluster could not be prepared, started or used.

    ``config_error`` by default: the operator (or the installer) must fix the
    environment - a retry with the same files would fail the same way. A start
    timeout is ``timeout`` (retryable: a slow disk may recover).
    """

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR

    def __init__(
        self,
        message: str,
        *,
        reason: ManagedDbReason,
        details: dict[str, object] | None = None,
        failure_type: FailureType | None = None,
    ) -> None:
        merged: dict[str, object] = {"reason": reason.value, **(details or {})}
        super().__init__(message, details=merged, failure_type=failure_type)
        self.reason = reason


class ClusterVersionMismatchError(ManagedDbError):
    """The data dir was created by another PostgreSQL major version.

    The controller refuses to start it: a different major cannot read the files,
    and silently re-running ``initdb`` would lose the fabric's database. The
    remedy is ``pg_upgrade`` (future) or restoring a ``pg_dump`` backup.
    """

    def __init__(self, *, data_dir: str, data_major: str, binary_major: str) -> None:
        super().__init__(
            f"the database in {data_dir} was created by PostgreSQL {data_major}, "
            f"but this MeshForge bundles PostgreSQL {binary_major}; refusing to "
            "start it. Restore a backup taken with pg_dump, or upgrade the data "
            "with pg_upgrade",
            reason=ManagedDbReason.VERSION_MISMATCH,
            details={
                "data_dir": data_dir,
                "data_major": data_major,
                "binary_major": binary_major,
            },
        )
        self.data_major = data_major
        self.binary_major = binary_major
