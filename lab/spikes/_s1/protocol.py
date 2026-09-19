"""S1 wire protocol, address safety rules, and typed measurement errors.

TCP (one connection per operation / per stream): the client sends one framed
header ``[4-byte big-endian length][UTF-8 JSON object]``; replies use the same
framing. Operations:

* ``hello``    -> ``{"ok", "protocol", "hostname", "os", "peer"}``
* ``upload``   (client -> server bulk bytes until the client half-closes; the
  server counts what it *received*, then closes)
* ``download`` (server -> client bulk bytes for ``seconds``; the server
  half-closes; the client counts what it received)
* ``result``   -> the server's per-stream view of a finished ``test_id``
  (receiver view for uploads, sender view for downloads)

UDP (same port number): the server echoes packets that start with
:data:`MAGIC`, byte for byte (no amplification), and only to private peers.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import re
import socket
import struct
from enum import StrEnum
from typing import Any, Final

PROTOCOL: Final = 1
#: Default TCP+UDP port (unassigned by IANA; not iperf3's 5201 so both can run).
DEFAULT_PORT: Final = 47201
MAGIC: Final = b"MFS1"
#: UDP ping: magic, sequence number, per-repetition token, reserved.
PING: Final = struct.Struct("!4sIQQ")
MIN_PING_BYTES: Final = PING.size
#: 1500-byte Ethernet MTU minus IPv4 + UDP headers: never fragments.
MAX_PING_BYTES: Final = 1472
MAX_HEADER_BYTES: Final = 4096
#: Application buffer handed to one ``send`` / ``recv_into`` call.
SEND_CHUNK_BYTES: Final = 128 * 1024
RECV_BUFFER_BYTES: Final = 256 * 1024
MAX_STREAMS: Final = 16
MAX_SECONDS: Final = 120.0
MIN_SECONDS: Final = 0.1
_TEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_LEN: Final = struct.Struct("!I")

type Message = dict[str, Any]


class FailureType(StrEnum):
    SERVER_UNREACHABLE = "server_unreachable"
    CONNECTION_LOST = "connection_lost"
    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol_error"
    SERVER_ERROR = "server_error"
    NO_REPLY = "no_reply"
    MEASUREMENT_INVALID = "measurement_invalid"


#: After one of these, the rest of a case is skipped and the server is re-checked.
FATAL: Final = frozenset(
    {
        FailureType.SERVER_UNREACHABLE,
        FailureType.CONNECTION_LOST,
        FailureType.TIMEOUT,
        FailureType.NO_REPLY,
    }
)


class MeasureError(Exception):
    """A measurement failed; ``failure_type`` says how."""

    def __init__(self, failure_type: FailureType, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type

    @property
    def fatal(self) -> bool:
        return self.failure_type in FATAL


def classify_os_error(exc: OSError, doing: str) -> MeasureError:
    """Map a socket error to a typed :class:`MeasureError`."""
    if isinstance(exc, TimeoutError):
        return MeasureError(FailureType.TIMEOUT, f"{doing}: timed out")
    if isinstance(exc, ConnectionRefusedError):
        return MeasureError(
            FailureType.SERVER_UNREACHABLE, f"{doing}: connection refused"
        )
    if isinstance(exc, ConnectionResetError | BrokenPipeError | ConnectionAbortedError):
        return MeasureError(FailureType.CONNECTION_LOST, f"{doing}: {exc}")
    unreachable = {
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EHOSTDOWN,
        errno.ENETDOWN,
    }
    if exc.errno in unreachable or isinstance(exc, socket.gaierror):
        return MeasureError(FailureType.SERVER_UNREACHABLE, f"{doing}: {exc}")
    return MeasureError(FailureType.CONNECTION_LOST, f"{doing}: {exc}")


class ProtocolError(ValueError):
    """The peer sent something that is not valid S1 protocol."""


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly ``size`` bytes; EOF before that raises ``ConnectionResetError``."""
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionResetError("peer closed the connection")
        buf += chunk
    return bytes(buf)


def send_msg(sock: socket.socket, msg: Message) -> None:
    body = json.dumps(msg, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(body) > MAX_HEADER_BYTES:
        raise ProtocolError("message too large")
    sock.sendall(_LEN.pack(len(body)) + body)


#: Size of the big-endian length prefix in front of every message.
LENGTH_BYTES: Final = _LEN.size


def body_length(prefix: bytes) -> int:
    """Body size from a 4-byte length prefix; ``ProtocolError`` if out of range."""
    (size,) = _LEN.unpack(prefix)
    if not 0 < size <= MAX_HEADER_BYTES:
        raise ProtocolError(f"bad header length {size}")
    return int(size)


def decode_body(body: bytes) -> Message:
    """A message body (UTF-8 JSON object); ``ProtocolError`` otherwise."""
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"header is not JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("header is not a JSON object")
    return msg


def recv_msg(sock: socket.socket) -> Message:
    size = body_length(recv_exact(sock, LENGTH_BYTES))
    return decode_body(recv_exact(sock, size))


def valid_test_id(value: object) -> bool:
    return isinstance(value, str) and bool(_TEST_ID_RE.match(value))


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    addr = ipaddress.ip_address(text)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_lan_address(text: str) -> bool:
    """Private (RFC 1918 / ULA), loopback or link-local; never public/multicast."""
    try:
        addr = _ip(text)
    except ValueError:
        return False
    if addr.is_multicast or addr.is_unspecified:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def is_allowed_peer(text: str) -> bool:
    """Clients the server talks to: LAN addresses only (docs/LAB.md §6)."""
    return is_lan_address(text)


def check_bind_address(text: str, *, allow_any: bool) -> str:
    """Validate a ``--bind`` value; returns the normalised address.

    An IP literal is required. The wildcard (``0.0.0.0`` / ``::``) and public
    addresses are refused unless ``allow_any`` - a spike server listens only on
    the one lab interface being measured.
    """
    try:
        addr = _ip(text.strip())
    except ValueError:
        raise ValueError(f"--bind needs an IP address, got {text!r}") from None
    if addr.is_unspecified and not allow_any:
        raise ValueError(
            f"refusing to bind the wildcard address {text}: pass this laptop's lab "
            "IP (e.g. 192.168.1.20), or --allow-any if you really mean every interface"
        )
    if not addr.is_unspecified and not is_lan_address(str(addr)) and not allow_any:
        raise ValueError(
            f"refusing to bind non-private address {text} (trusted LAN only, "
            "docs/LAB.md §6); pass --allow-any to override"
        )
    return str(addr)


def family_of(host: str) -> socket.AddressFamily:
    try:
        return socket.AF_INET6 if _ip(host).version == 6 else socket.AF_INET
    except ValueError:
        return socket.AF_INET
