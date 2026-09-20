"""S1 wire protocol and typed measurement errors.

Framing and the LAN-only address rules live in :mod:`spikes._harness.protocol`
(shared by S2/S3) and are re-exported here for S1's own modules and tests.

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
import re
import socket
import struct
from enum import StrEnum
from typing import Final

from spikes._harness.protocol import (
    LENGTH_BYTES,
    MAX_HEADER_BYTES,
    Message,
    ProtocolError,
    body_length,
    check_bind_address,
    decode_body,
    family_of,
    is_allowed_peer,
    is_lan_address,
    recv_exact,
    recv_msg,
    send_msg,
)

PROTOCOL: Final = 1
#: Default TCP+UDP port (unassigned by IANA; not iperf3's 5201 so both can run).
DEFAULT_PORT: Final = 47201
MAGIC: Final = b"MFS1"
#: UDP ping: magic, sequence number, per-repetition token, reserved.
PING: Final = struct.Struct("!4sIQQ")
MIN_PING_BYTES: Final = PING.size
#: 1500-byte Ethernet MTU minus IPv4 + UDP headers: never fragments.
MAX_PING_BYTES: Final = 1472
#: Application buffer handed to one ``send`` / ``recv_into`` call.
SEND_CHUNK_BYTES: Final = 128 * 1024
RECV_BUFFER_BYTES: Final = 256 * 1024
MAX_STREAMS: Final = 16
MAX_SECONDS: Final = 120.0
MIN_SECONDS: Final = 0.1
_TEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")

__all__ = [
    "DEFAULT_PORT",
    "FATAL",
    "LENGTH_BYTES",
    "MAGIC",
    "MAX_HEADER_BYTES",
    "MAX_PING_BYTES",
    "MAX_SECONDS",
    "MAX_STREAMS",
    "MIN_PING_BYTES",
    "MIN_SECONDS",
    "PING",
    "PROTOCOL",
    "RECV_BUFFER_BYTES",
    "SEND_CHUNK_BYTES",
    "FailureType",
    "MeasureError",
    "Message",
    "ProtocolError",
    "body_length",
    "check_bind_address",
    "classify_os_error",
    "decode_body",
    "family_of",
    "is_allowed_peer",
    "is_lan_address",
    "recv_exact",
    "recv_msg",
    "send_msg",
    "valid_test_id",
]


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


def valid_test_id(value: object) -> bool:
    return isinstance(value, str) and bool(_TEST_ID_RE.match(value))
