"""The laptop's friendly name (V0.17 G3, Q27): ``<data_dir>/node_name``.

The owner sets it in the console; the agent sends it as ``Hello.display_name``
at its next Hello (every reconnect builds a new Hello, so a change shows in the
dashboard after the next reconnect - the console offers the restart). The file,
when present, overrides ``[agent] node_name`` - also when it is empty: the owner
cleared the name on purpose. It is private like every file the agent writes
(``0600``, read back with :func:`~meshforge.common.private_files.read_private`).

Validation is :func:`~meshforge.agent.settings.check_node_name` (printable, at
most 64 characters), the same rule as the configuration key and the controller.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Final

from meshforge.agent.settings import AgentSettings, check_node_name
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import ConfigError
from meshforge.common.private_files import (
    PrivateFileError,
    PrivateFileReason,
    protect_private,
    read_private,
)

__all__ = [
    "NODE_NAME_FILE_NAME",
    "clear_node_name",
    "effective_node_name",
    "node_name_path",
    "read_node_name",
    "write_node_name",
]

NODE_NAME_FILE_NAME: Final = "node_name"
_MAX_BYTES: Final = 1024


def node_name_path(settings: AgentSettings) -> Path:
    return settings.data_dir / NODE_NAME_FILE_NAME


def read_node_name(path: Path) -> str | None:
    """The stored name (``None`` = no file). :class:`ConfigError` when the file
    is unsafe (not private, a symlink, ...) or not a valid name."""
    try:
        raw = read_private(path, limit=_MAX_BYTES, what="node name file")
    except PrivateFileError as exc:
        if exc.reason is PrivateFileReason.MISSING:
            return None
        raise ConfigError(exc.message, details={"reason": exc.reason.value}) from None
    try:
        return check_node_name(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(
            f"the node name file {path} is not a valid name: {exc}",
            details={"reason": "invalid_value"},
        ) from None


def effective_node_name(settings: AgentSettings) -> str:
    """The name to send: the console's file, else ``[agent] node_name``. A
    broken file falls back to the configuration (the Hello must still go out;
    the console shows the file's problem)."""
    try:
        stored = read_node_name(node_name_path(settings))
    except ConfigError:
        return settings.agent.node_name
    return settings.agent.node_name if stored is None else stored


def write_node_name(settings: AgentSettings, value: str) -> str:
    """Validate and store ``value`` (atomic, ``0600``); returns the stored name.
    :class:`ValueError` for an invalid name, :class:`OSError` when it cannot be
    written."""
    name = check_node_name(value)
    path = node_name_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, name.encode("utf-8"), mode=0o600)
    protect_private(path)
    return name


def clear_node_name(settings: AgentSettings) -> bool:
    """Remove the file (back to ``[agent] node_name``); True if one existed."""
    with contextlib.suppress(FileNotFoundError):
        node_name_path(settings).unlink()
        return True
    return False
