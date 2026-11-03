"""The last-known controller endpoint (V1.0 G8 F4): a dial hint beside the identity.

Why: an agent with an empty ``agent.controller_address`` found its controller
only by mDNS. On a laptop where mDNS answers never arrive (lab L4: Windows 11,
UDP 5353 multicast filtered on that Wi-Fi adapter / by the firewall for the
service account), a successfully *joined* agent looped DISCOVERING -> BACKOFF
forever although the controller was reachable by unicast.

What: ``controller.json`` in the identity directory (machine-wide on a Windows
service install; never the owner-edited ``config.toml``)::

    {
        "format": 1,
        "address": "192.168.31.13:47100",
        "source": "join" | "session",
        "updated_at": "<ISO 8601 UTC>",
    }

* written at **join** - the host the agent enrolled against plus the session
  port the controller names in ``EnrollResponse.session_port``;
* rewritten after every **accepted session** whose address differs - so when
  the controller's IP changes (DHCP) and mDNS finds the new one, the hint
  follows. Only an *accepted* session writes it, i.e. a controller that passed
  mTLS against the pinned fabric CA;
* deleted with the enrolment (``identity.reset`` - leave / reset) and replaced
  (or deleted) by a re-join, so a hint never outlives its fabric.

**Trust.** The address only says where to dial. Trust is unchanged: the
session's TLS still verifies the controller against the fabric CA and presents
the node certificate, exactly as for an mDNS or configured address.

Reads use the identity files' rules (``private_files.read_private``: a
regular owner-only file, no symlink/FIFO/device, bounded); addresses are
validated strictly on write and on read. Writes run under the identity lock
(the lock-holder's temp-file sweep must never see an in-flight write) and are
atomic. Every failure here is logged and swallowed: losing the hint degrades
to mDNS-only, it never stops the agent. Blocking - call from a thread in async code.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from meshforge.agent.identity import (
    CONTROLLER_FILE_NAME,
    DEFAULT_LOCK_TIMEOUT_S,
    locked_identity,
)
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.private_files import (
    PrivateFileError,
    PrivateFileReason,
    read_private,
)

__all__ = [
    "KNOWN_CONTROLLER_FORMAT",
    "KnownController",
    "forget",
    "load_known",
    "remember",
]

log = get_logger(__name__)

KNOWN_CONTROLLER_FORMAT: Final = 1
_MAX_BYTES: Final = 4096
_FILE_MODE: Final = 0o600

Source = Literal["join", "session"]


@dataclass(frozen=True, slots=True)
class KnownController:
    address: str
    """``host:port`` (IPv6 bracketed) - a gRPC target."""
    source: str
    updated_at: str


def _describe(exc: OSError | MeshForgeError) -> str:
    """A short cause without a path (a path would name the user, checklist 7)."""
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    return type(exc).__name__


def _path(directory: Path) -> Path:
    return Path(directory) / CONTROLLER_FILE_NAME


_HOST_NAME_RE: Final = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)


def _valid_address(address: str) -> bool:
    """Strict: a ``host:port`` that is already in canonical gRPC-target form
    (IPv6 bracketed, nothing around it) whose host is an IP literal or a plain
    DNS name - no whitespace, control characters or other junk (F4 D3)."""
    from meshforge.agent.discovery import format_target
    from meshforge.agent.settings import parse_controller_address

    try:
        host, port = parse_controller_address(address)
    except ValueError:
        return False
    if format_target(host, port) != address:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return _HOST_NAME_RE.fullmatch(host) is not None
    return True


def load_known(directory: Path) -> KnownController | None:
    """The stored hint, or ``None`` (missing, unreadable or malformed - the
    last two are logged; the caller just falls back to "not found")."""
    # Same rules as the identity files (F4 D1/D2): a regular, owner-only file;
    # never a symlink, FIFO or device - opened O_NONBLOCK | O_NOFOLLOW, so a
    # planted FIFO cannot hang the resolver thread.
    try:
        raw = read_private(_path(directory), limit=_MAX_BYTES, what="controller hint")
    except PrivateFileError as exc:
        if exc.reason is PrivateFileReason.MISSING:
            return None
        if exc.reason is PrivateFileReason.OVERSIZED:
            log.warning("agent.known_controller.ignored", reason="too large")
        else:
            log.warning("agent.known_controller.unreadable", error=exc.reason.value)
        return None
    problem: str | None = None
    data: object = None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        problem = "not JSON"
    if problem is None:
        if not isinstance(data, dict) or data.get("format") != KNOWN_CONTROLLER_FORMAT:
            problem = "unknown format"
        elif not isinstance(data.get("address"), str) or not _valid_address(
            data["address"]
        ):
            problem = "bad address"
    if problem is not None or not isinstance(data, dict):
        log.warning("agent.known_controller.ignored", reason=problem)
        return None
    return KnownController(
        address=str(data["address"]),
        source=str(data.get("source", "")),
        updated_at=str(data.get("updated_at", "")),
    )


def remember(
    directory: Path,
    address: str,
    *,
    source: Source,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    node_id: str | None = None,
) -> bool:
    """Store ``address`` as the hint (no write when it is already stored).
    True = the file now names ``address``; False = it could not be written
    (logged). ``address`` must be a valid ``host:port``."""
    if not _valid_address(address):
        log.warning(
            "agent.known_controller.not_saved", node_id=node_id, reason="bad address"
        )
        return False
    body = {
        "format": KNOWN_CONTROLLER_FORMAT,
        "address": address,
        "source": source,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    data = (json.dumps(body, sort_keys=True) + "\n").encode("ascii")
    try:
        with locked_identity(directory, lock_timeout_s=lock_timeout_s):
            current = load_known(directory)
            if current is not None and current.address == address:
                return True
            atomic_write_bytes(_path(directory), data, mode=_FILE_MODE)
    except (OSError, MeshForgeError) as exc:
        log.warning(
            "agent.known_controller.not_saved",
            node_id=node_id,
            error=_describe(exc),
        )
        return False
    log.info(
        "agent.known_controller.saved",
        node_id=node_id,
        address=address,
        source=source,
        previous=None if current is None else current.address,
    )
    return True


def forget(
    directory: Path,
    *,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    node_id: str | None = None,
) -> None:
    """Delete the hint (missing is fine; a failure is logged)."""
    try:
        with locked_identity(directory, lock_timeout_s=lock_timeout_s):
            _path(directory).unlink(missing_ok=True)
    except (OSError, MeshForgeError) as exc:
        log.warning(
            "agent.known_controller.not_deleted",
            node_id=node_id,
            error=_describe(exc),
        )
