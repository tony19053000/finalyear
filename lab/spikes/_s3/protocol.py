"""S3 control protocol, size/chunk rules, address safety and typed failures.

All three transports move the *same* deterministic payload (:mod:`.payload`) and
are driven by one small control protocol on the raw-TCP port:

* ``hello``    -> ``{"ok", "protocol", "hostname", "name", "os", "peer"}``
* ``download`` -> the server streams ``size`` bytes in ``chunk``-sized writes and
  closes; the client counts and verifies what it received
* ``upload``   -> the client streams ``size`` bytes and half-closes; the server
  counts + verifies and replies with its **receiver view** (framed JSON)
* ``stats``    -> the server's view of a finished transfer (any transport):
  bytes, CPU seconds spent in this process during the transfer window, peak RSS

gRPC uses the same request/result JSON as opaque ``bytes`` messages over generic
handlers (no ``.proto`` file is added for a spike); HTTP uses
``GET /blob?...`` with optional ``Range``. Framing of control messages (4-byte
big-endian length + UTF-8 JSON) and the address rules of docs/LAB.md §6 are
reused from S1 rather than re-implemented.
"""

from __future__ import annotations

import errno
import re
import socket
from enum import StrEnum
from typing import Any, Final

from spikes._s1.protocol import (
    MAX_HEADER_BYTES,
    ProtocolError,
    check_bind_address,
    family_of,
    is_lan_address,
    recv_exact,
    recv_msg,
    send_msg,
)

__all__ = [
    "MAX_HEADER_BYTES",
    "ProtocolError",
    "TransferError",
    "check_bind_address",
    "family_of",
    "is_lan_address",
    "recv_exact",
    "recv_msg",
    "send_msg",
]

PROTOCOL: Final = 1
#: Raw-TCP control+data port; gRPC and HTTP use the next two port numbers.
DEFAULT_PORT: Final = 47301
GRPC_PORT_OFFSET: Final = 1
HTTP_PORT_OFFSET: Final = 2
PORT_SPAN: Final = 3

GRPC_SERVICE: Final = "meshforge.lab.s3.Transport"
GRPC_DOWNLOAD: Final = f"/{GRPC_SERVICE}/Download"
GRPC_UPLOAD: Final = f"/{GRPC_SERVICE}/Upload"
HTTP_BLOB_PATH: Final = "/blob"

KIB: Final = 1024
MIB: Final = 1024 * 1024
GIB: Final = 1024 * 1024 * 1024

#: Payload sizes of the spike (docs/LAB.md §3: "1 MB -> 2 GB"; binary units).
DEFAULT_SIZES: Final = (1 * MIB, 8 * MIB, 64 * MIB, 256 * MIB, 1 * GIB, 2 * GIB)
MIN_SIZE_BYTES: Final = 4 * KIB
MAX_SIZE_BYTES: Final = 4 * GIB
#: Application write size (also the gRPC message size). Capped by the payload block.
DEFAULT_CHUNK_BYTES: Final = 1 * MIB
MIN_CHUNK_BYTES: Final = 4 * KIB
MAX_CHUNK_BYTES: Final = 4 * MIB
#: gRPC chunk sizes swept at ``--chunk-sweep-size``.
CHUNK_SWEEP: Final = (64 * KIB, 256 * KIB, 1 * MIB, 4 * MIB)
#: gRPC's own default for ``grpc.max_receive_message_length`` (4 MB, not MiB).
GRPC_DEFAULT_LIMIT: Final = 4 * 1024 * 1024
#: Slack over the chunk size for gRPC's max_{send,receive}_message_length.
GRPC_LIMIT_SLACK: Final = 64 * KIB
#: Parallel HTTP range requests in the "range" case (ADR-0003: parallel chunks).
DEFAULT_RANGE_PARTS: Final = 4
MAX_RANGE_PARTS: Final = 16

_ID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SIZE_RE: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")
_UNITS: Final = {
    "": 1,
    "b": 1,
    "k": KIB,
    "kb": 1000,
    "kib": KIB,
    "m": MIB,
    "mb": 1000 * 1000,
    "mib": MIB,
    "g": GIB,
    "gb": 1000 * 1000 * 1000,
    "gib": GIB,
}


class FailureType(StrEnum):
    """Typed failure categories recorded in the result JSON."""

    SERVER_UNREACHABLE = "server_unreachable"
    CONNECTION_LOST = "connection_lost"
    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol_error"
    SERVER_ERROR = "server_error"
    TRANSPORT_ERROR = "transport_error"
    SHORT_TRANSFER = "short_transfer"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    NO_STATS = "no_stats"
    #: grpcio is not importable on this side (or the server runs without it)
    GRPC_UNAVAILABLE = "grpc_unavailable"


#: After one of these the rest of the case is skipped and the server re-checked.
FATAL: Final = frozenset(
    {
        FailureType.SERVER_UNREACHABLE,
        FailureType.CONNECTION_LOST,
        FailureType.TIMEOUT,
    }
)


class TransferError(Exception):
    """A measured transfer failed; ``failure_type`` says how."""

    def __init__(self, failure_type: FailureType, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type

    @property
    def fatal(self) -> bool:
        return self.failure_type in FATAL


def classify_os_error(exc: OSError, doing: str) -> TransferError:
    """Map a socket error to a typed :class:`TransferError`."""
    if isinstance(exc, TimeoutError):
        return TransferError(FailureType.TIMEOUT, f"{doing}: timed out")
    if isinstance(exc, ConnectionRefusedError):
        return TransferError(
            FailureType.SERVER_UNREACHABLE, f"{doing}: connection refused"
        )
    if isinstance(exc, ConnectionResetError | BrokenPipeError | ConnectionAbortedError):
        return TransferError(FailureType.CONNECTION_LOST, f"{doing}: {exc}")
    unreachable = {
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EHOSTDOWN,
        errno.ENETDOWN,
    }
    if exc.errno in unreachable or isinstance(exc, socket.gaierror):
        return TransferError(FailureType.SERVER_UNREACHABLE, f"{doing}: {exc}")
    return TransferError(FailureType.CONNECTION_LOST, f"{doing}: {exc}")


# ------------------------------------------------------------------ sizes
def parse_size(text: str) -> int:
    """``"64MiB"``/``"2GiB"``/``"1MB"``/``"1048576"`` -> bytes (binary KiB/MiB/GiB,
    decimal KB/MB/GB). Raises ``ValueError`` outside the allowed range."""
    match = _SIZE_RE.match(text)
    if match is None:
        raise ValueError(f"not a byte size: {text!r} (e.g. 64MiB, 1GiB, 1048576)")
    number, unit = match.group(1), match.group(2).lower()
    if unit not in _UNITS:
        raise ValueError(
            f"unknown unit {unit!r} in {text!r} (B, KiB, MiB, GiB, KB, MB, GB)"
        )
    value = int(float(number) * _UNITS[unit])
    if not MIN_SIZE_BYTES <= value <= MAX_SIZE_BYTES:
        raise ValueError(
            f"size {text!r} outside {format_size(MIN_SIZE_BYTES)}..{format_size(MAX_SIZE_BYTES)}"
        )
    return value


def parse_sizes(text: str) -> tuple[int, ...]:
    """Comma-separated sizes, sorted and de-duplicated."""
    values = sorted({parse_size(part) for part in text.split(",") if part.strip()})
    if not values:
        raise ValueError("no sizes given")
    return tuple(values)


def parse_chunk(text: str) -> int:
    value = parse_size(text)
    if not MIN_CHUNK_BYTES <= value <= MAX_CHUNK_BYTES:
        raise ValueError(
            f"chunk {text!r} outside {format_size(MIN_CHUNK_BYTES)}.."
            f"{format_size(MAX_CHUNK_BYTES)}"
        )
    return value


def format_size(num_bytes: int) -> str:
    """Short binary label used in case ids and tables (``64MiB``, ``2GiB``)."""
    for unit, scale in (("GiB", GIB), ("MiB", MIB), ("KiB", KIB)):
        if num_bytes >= scale and num_bytes % scale == 0:
            return f"{num_bytes // scale}{unit}"
    return f"{num_bytes}B"


def select_sizes(sizes: tuple[int, ...], max_size: int) -> tuple[int, ...]:
    """The sizes a laptop should actually run: everything up to ``max_size``.

    Always keeps at least the smallest requested size, so a very small
    ``--max-size`` still produces a comparable data point instead of nothing.
    """
    kept = tuple(s for s in sizes if s <= max_size)
    return kept or (min(sizes),)


def grpc_limit(chunk: int) -> int:
    """``max_{send,receive}_message_length`` needed for ``chunk``-sized messages."""
    return chunk + GRPC_LIMIT_SLACK


def grpc_target(host: str, port: int) -> str:
    """gRPC address literal (IPv6 hosts are bracketed)."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def peer_host(peer: str) -> str | None:
    """Host part of a gRPC ``context.peer()`` string, or ``None`` if unknown.

    ``ipv4:10.0.0.2:53422`` -> ``10.0.0.2``; ``ipv6:[::1]:53422`` -> ``::1``.
    gRPC percent-encodes the ``%`` of an IPv6 scope id (``%25eth0``), which is
    undone so ``ipaddress`` can parse a link-local peer.
    """
    scheme, _, rest = peer.partition(":")
    if scheme not in {"ipv4", "ipv6"} or not rest:
        return None
    if rest.startswith("["):
        host, sep, _ = rest[1:].partition("]")
        return host.replace("%25", "%") if sep else None
    if ":" not in rest:
        return rest
    host, _, _port = rest.rpartition(":")
    return host or None


# --------------------------------------------------------------- messages
type Message = dict[str, Any]


def valid_transfer_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _int_field(msg: Message, key: str, lo: int, hi: int) -> int:
    value = msg.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ProtocolError(f"bad {key}")
    return value


def transfer_request(msg: Message) -> tuple[str, int, int]:
    """Validate a download/upload request -> ``(transfer_id, size, chunk)``.

    Raises :class:`ProtocolError` for anything out of range, so a garbage or
    hostile request costs one dropped connection, never a huge allocation.
    """
    if not valid_transfer_id(msg.get("transfer_id")):
        raise ProtocolError("bad transfer_id")
    size = _int_field(msg, "size", MIN_SIZE_BYTES, MAX_SIZE_BYTES)
    chunk = _int_field(msg, "chunk", MIN_CHUNK_BYTES, MAX_CHUNK_BYTES)
    return str(msg["transfer_id"]), size, chunk
