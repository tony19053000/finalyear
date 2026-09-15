"""Typed store errors (messages never contain the password)."""

from __future__ import annotations

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = ["DatabaseUnavailableError"]


class DatabaseUnavailableError(MeshForgeError):
    """PostgreSQL could not be reached or rejected the connection.

    ``details["url"]`` holds the password-masked URL. Retryable by default
    (``network_error``): the database may simply not be up yet.
    """

    default_failure_type = FailureType.NETWORK_ERROR
