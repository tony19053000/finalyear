"""Which node is *this* laptop? Read-only lookup of the local agent's ``node_id`` (D50).

``compute run --local`` places a job on the submitting laptop's own agent. The CLI
must not import ``meshforge.agent``, so this module reads the agent's
``<data_dir>/identity/node.json`` (ADR-0013 format 1: ``{"format": 1, "node_id",
...}``) itself - read-only, bounded, never the key file, never a lock (reading a
JSON file the agent replaces atomically cannot observe a half-written state).

Failures are typed :class:`LocalNodeError`: ``not_found`` when this laptop has no
agent identity yet (the agent never ran here), ``config_error`` when the file is
there but unreadable or not a format-1 identity.

**The packaged system agent (V1.0 G11, D457).** A ``.deb`` / Windows-installer
agent keeps its identity in the machine-wide data dir (``/var/lib/meshforge``,
``%ProgramData%\\MeshForge``), which only the service account can read - so the
person typing ``compute run --local`` cannot open its ``node.json``, and must not
be allowed to (the key sits next to it). Instead the agent publishes a
**world-readable public record** at start (``agent/public_node.py``)::

    <public dir>/node-id.json   {"format": 1, "node_id", "key_fingerprint_sha256",
                                 "agent_version", "written_at"}

Only public facts: the ``node_id`` (shown by ``compute nodes`` anyway), the
SHA-256 of the *public* key and the version. The public dir is
:func:`system_public_dir`: ``/var/lib/meshforge-public`` on Linux (a sibling of
the private state dir, so ``/var/lib/meshforge`` stays ``0750``; ``meshforge``
``0755``, created by the package) and ``%ProgramData%\\MeshForge\\public`` on
Windows (the installer's ACL lets Users read it).

:func:`resolve_local_node` is what the CLI calls: the user-mode identity in the
CLI's own data dir first (a developer's ``meshforge-agent run``), then the system
agent's public record. A private file the caller cannot open (not there, or
owner-only) is skipped silently - nothing else in the private dir is touched -
and when nothing usable is found the most telling error wins (a garbled file
over "no identity").
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Final

from meshforge.common.errors import FailureType, InvalidIdError, MeshForgeError
from meshforge.common.ids import NodeId, validate_node_id

__all__ = [
    "AGENT_CAS_DIR_NAME",
    "AGENT_GC_DIR_NAME",
    "IDENTITY_DIR_NAME",
    "IDENTITY_FORMAT",
    "MAX_NODE_JSON_BYTES",
    "NODE_JSON_NAME",
    "PUBLIC_NODE_FILE_NAME",
    "PUBLIC_NODE_FORMAT",
    "SYSTEM_LINUX_PUBLIC_DIR",
    "LocalNode",
    "LocalNodeError",
    "LocalNodeSource",
    "local_node_json",
    "public_node_json",
    "read_local_node_id",
    "read_public_node_id",
    "resolve_local_node",
    "system_public_dir",
]

IDENTITY_DIR_NAME: Final = "identity"
AGENT_CAS_DIR_NAME: Final = "cas"
"""``<data_dir>/cas``: the agent's blob cache (ARCHITECTURE §23); ``compute run``
seeds it for the Local candidate (V0.6 D109)."""
AGENT_GC_DIR_NAME: Final = "gc"
"""``<data_dir>/gc``: the agent's GC last-use index next to its cache."""
NODE_JSON_NAME: Final = "node.json"
IDENTITY_FORMAT: Final = 1
MAX_NODE_JSON_BYTES: Final = 64 * 1024
"""node.json is ~250 bytes; anything this large is not ours."""
PUBLIC_NODE_FILE_NAME: Final = "node-id.json"
PUBLIC_NODE_FORMAT: Final = 1
SYSTEM_LINUX_PUBLIC_DIR: Final = PurePosixPath("/var/lib/meshforge-public")
"""D457: the Linux system agent's public dir (``meshforge`` ``0755``, made by the
``.deb`` postinst; the service unit lists it in ``ReadWritePaths``)."""
_WINDOWS_PROGRAM_DATA_DEFAULT: Final = PureWindowsPath("C:/ProgramData")
_WINDOWS_PUBLIC_SUBDIR: Final = ("MeshForge", "public")


class LocalNodeSource(StrEnum):
    """Where :func:`resolve_local_node` found this laptop's node."""

    USER = "user"
    """The CLI's own data dir: an agent run by this user (or the CLI acting in
    system mode on the service's own state)."""
    SYSTEM = "system"
    """The packaged system agent's public record (D457)."""


@dataclass(frozen=True, slots=True)
class LocalNode:
    node_id: NodeId
    source: LocalNodeSource


class LocalNodeError(MeshForgeError):
    """No usable local agent identity."""

    default_failure_type = FailureType.CONFIG_ERROR


def local_node_json(data_dir: Path) -> Path:
    """``<data_dir>/identity/node.json`` (the same layout the agent writes)."""
    return data_dir / IDENTITY_DIR_NAME / NODE_JSON_NAME


_NO_IDENTITY: Final = (
    "this laptop has no MeshForge agent identity yet: start "
    "`meshforge-agent run` here once, or start the installed MeshForge agent "
    "service (or use --node NODE_ID)"
)


def read_local_node_id(data_dir: Path) -> NodeId:
    """The ``node_id`` of the agent that uses ``data_dir`` on this machine."""
    path = local_node_json(data_dir)
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_NODE_JSON_BYTES + 1)
    except FileNotFoundError:
        raise LocalNodeError(_NO_IDENTITY, failure_type=FailureType.NOT_FOUND) from None
    except PermissionError as exc:
        # D457: an owner-only machine-wide identity - never ours to read
        raise LocalNodeError(
            f"cannot read the local agent identity: {exc.strerror or exc}",
            details={"unreadable": True},
        ) from None
    except OSError as exc:
        raise LocalNodeError(
            f"cannot read the local agent identity: {exc.strerror or exc}"
        ) from None
    return _parse(raw, IDENTITY_FORMAT, "the local agent identity file")


def system_public_dir(
    platform: str | None = None, environ: Mapping[str, str] | None = None
) -> PurePath | None:
    """Where the packaged system agent publishes its public record (D457);
    ``None`` where there is no system agent (macOS)."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform.startswith("win"):
        value = environ.get("PROGRAMDATA", "").strip()
        base = PureWindowsPath(value) if value else _WINDOWS_PROGRAM_DATA_DEFAULT
        if not base.is_absolute():
            base = _WINDOWS_PROGRAM_DATA_DEFAULT
        return base.joinpath(*_WINDOWS_PUBLIC_SUBDIR)
    if platform == "darwin":
        return None
    return SYSTEM_LINUX_PUBLIC_DIR


def public_node_json(public_dir: Path) -> Path:
    """``<public dir>/node-id.json``."""
    return public_dir / PUBLIC_NODE_FILE_NAME


def read_public_node_id(public_dir: Path) -> NodeId:
    """The ``node_id`` in the system agent's public record (D457).

    On POSIX a record (or its folder) that anyone but its owner may write is
    refused: whoever could rewrite it would decide where ``--local`` jobs run.
    """
    path = public_node_json(public_dir)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if os.name == "posix":
            _check_not_shared_writable(public_dir, os.stat(public_dir), "folder")
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise LocalNodeError(_NO_IDENTITY, failure_type=FailureType.NOT_FOUND) from None
    except OSError as exc:
        raise LocalNodeError(
            f"cannot read the MeshForge agent's public node record {path}: "
            f"{exc.strerror or exc}"
        ) from None
    with os.fdopen(fd, "rb") as fh:
        if os.name == "posix":
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise LocalNodeError(f"{path} is not a regular file")
            _check_not_shared_writable(path, st, "file")
        raw = fh.read(MAX_NODE_JSON_BYTES + 1)
    return _parse(raw, PUBLIC_NODE_FORMAT, f"the public node record {path}")


def resolve_local_node(data_dir: Path, *, public_dir: Path | None) -> LocalNode:
    """This laptop's node: the user-mode identity in ``data_dir``, else the
    system agent's public record in ``public_dir`` (module docstring)."""
    errors: list[LocalNodeError] = []
    try:
        return LocalNode(read_local_node_id(data_dir), LocalNodeSource.USER)
    except LocalNodeError as exc:
        if not _skippable_private(exc):
            errors.append(exc)
    if public_dir is not None:
        try:
            return LocalNode(read_public_node_id(public_dir), LocalNodeSource.SYSTEM)
        except LocalNodeError as exc:
            errors.append(exc)
    telling = [e for e in errors if e.failure_type is not FailureType.NOT_FOUND]
    if telling:
        raise telling[0]
    raise LocalNodeError(_NO_IDENTITY, failure_type=FailureType.NOT_FOUND)


def _skippable_private(exc: LocalNodeError) -> bool:
    """Not there, or not ours to read (an owner-only machine-wide identity):
    the public record decides."""
    return exc.failure_type is FailureType.NOT_FOUND or bool(
        exc.details.get("unreadable", False)
    )


def _check_not_shared_writable(path: Path, st: os.stat_result, what: str) -> None:
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise LocalNodeError(
            f"refusing the public node record: its {what} {path} is writable by "
            "other users (reinstall the MeshForge agent package)"
        )


def _parse(raw: bytes, fmt: int, what: str) -> NodeId:
    if len(raw) > MAX_NODE_JSON_BYTES:
        raise LocalNodeError(f"{what} is too large")
    try:
        meta = json.loads(raw)
    except (ValueError, RecursionError):
        raise LocalNodeError(f"{what} is not JSON") from None
    if (
        not isinstance(meta, dict)
        or type(meta.get("format")) is not int
        or meta.get("format") != fmt
    ):
        raise LocalNodeError(
            f"{what} has an unknown format; upgrade compute or the agent"
        )
    try:
        return validate_node_id(meta.get("node_id"))
    except InvalidIdError:
        raise LocalNodeError(f"{what} holds no valid node_id") from None
