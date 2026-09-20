"""Typed store errors (messages never contain the password)."""

from __future__ import annotations

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = ["DatabaseUnavailableError", "MigrationError"]


class DatabaseUnavailableError(MeshForgeError):
    """PostgreSQL could not be reached or rejected the connection.

    ``details["url"]`` holds the password-masked URL. Retryable by default
    (``network_error``): the database may simply not be up yet.
    """

    default_failure_type = FailureType.NETWORK_ERROR


class MigrationError(MeshForgeError):
    """``alembic upgrade head`` failed; the controller must not run on a stale schema.

    ``details`` carries ``target_revision`` (the head we tried to reach) and
    ``current_revision`` (what the database reported before the attempt, or ``None``
    for an empty database / unreachable database). Exit code ``CONFIG_ERROR``:
    the operator must fix the database before the controller can start.
    """

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR
