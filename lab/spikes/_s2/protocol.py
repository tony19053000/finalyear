"""S2 wire contract, keepalive option sets, typed failures and their fixes.

Two listeners, two ports (a TCP port carries one protocol): stdlib
``http.server`` on :data:`DEFAULT_HTTP_PORT` and a gRPC server on
:data:`DEFAULT_GRPC_PORT`. One contiguous range keeps the temporary firewall
rule to a single entry (docs/LAB.md §6).

HTTP paths:

* ``GET /ping``  -> small JSON body with the server's identity.
* ``GET /blob?bytes=N`` -> ``N`` bytes streamed in :data:`CHUNK_BYTES` pieces
  (``Content-Length`` set, so the client can also see a truncated transfer).

gRPC service ``meshforge.lab.v1.S2Probe`` is served through *generic handlers*
with byte pass-through (no serializer): the spike must run after a plain
``uv sync`` without generating code, and ``proto/`` is the product contract,
not a lab playground. Methods:

* ``Echo``     unary-unary, JSON in / JSON out (identity + round trip).
* ``Download`` unary-stream, JSON request -> raw byte chunks (streaming).
* ``Session``  stream-stream, the ADR-0002 shape: the *client* dials out and
  keeps one long-lived stream with messages flowing both ways.

Every message on ``Echo``/``Session`` is a UTF-8 JSON object, capped at
:data:`MAX_MESSAGE_BYTES`.
"""

from __future__ import annotations

import errno
import json
import socket
from enum import StrEnum
from typing import Any, Final

PROTOCOL: Final = 1
#: Unassigned by IANA; next to S1's 47201 so one firewall rule covers the lab.
DEFAULT_HTTP_PORT: Final = 47202
DEFAULT_GRPC_PORT: Final = 47203
SERVICE: Final = "meshforge.lab.v1.S2Probe"
METHOD_ECHO: Final = f"/{SERVICE}/Echo"
METHOD_DOWNLOAD: Final = f"/{SERVICE}/Download"
METHOD_SESSION: Final = f"/{SERVICE}/Session"

#: One application write / read on the bulk paths.
CHUNK_BYTES: Final = 64 * 1024
#: Largest control message (JSON) either side accepts.
MAX_MESSAGE_BYTES: Final = 64 * 1024
#: Largest ``/blob`` or ``Download`` payload a peer may ask for.
MAX_BLOB_BYTES: Final = 512 * 1024 * 1024
MIN_BLOB_BYTES: Final = 1
#: Longest a single bidi session may be held (server-side hard stop).
MAX_HOLD_S: Final = 3600.0

type Message = dict[str, Any]


# ----------------------------------------------------------------- keepalive
#: HTTP/2 keepalive the *client* uses when ``--keepalive on`` (ADR-0002: the
#: agent side decides, because it owns the long-lived stream).
CLIENT_KEEPALIVE_ON: Final[tuple[tuple[str, int], ...]] = (
    ("grpc.keepalive_time_ms", 20_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    # send pings even when no RPC is in flight (an idle Session is exactly that)
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    ("grpc.http2.min_time_between_pings_ms", 10_000),
)
#: ``--keepalive off``: gRPC's default, which sends no HTTP/2 pings at all.
CLIENT_KEEPALIVE_OFF: Final[tuple[tuple[str, int], ...]] = ()
#: The server must tolerate those pings, or it answers GOAWAY "too_many_pings"
#: and the test would measure our own configuration, not the network.
SERVER_KEEPALIVE: Final[tuple[tuple[str, int], ...]] = (
    ("grpc.keepalive_time_ms", 20_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.min_ping_interval_without_data_ms", 5_000),
    ("grpc.http2.min_recv_ping_interval_without_data_ms", 5_000),
)
KEEPALIVE_MODES: Final = ("on", "off")


def channel_options(keepalive: str) -> list[tuple[str, int]]:
    """Client channel options for ``keepalive`` (``"on"`` / ``"off"``)."""
    if keepalive not in KEEPALIVE_MODES:
        raise ValueError(
            f"keepalive must be one of {KEEPALIVE_MODES}, got {keepalive!r}"
        )
    base: list[tuple[str, int]] = [
        ("grpc.max_receive_message_length", MAX_BLOB_BYTES),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ]
    on = keepalive == "on"
    return base + list(CLIENT_KEEPALIVE_ON if on else CLIENT_KEEPALIVE_OFF)


def server_options() -> list[tuple[str, int]]:
    return [
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_send_message_length", MAX_BLOB_BYTES),
        *SERVER_KEEPALIVE,
    ]


def keepalive_settings(keepalive: str) -> dict[str, Any]:
    """What the report must show: the exact options both sides ran with."""
    return {
        "mode": keepalive,
        "client_options": dict(channel_options(keepalive)),
        "server_options": dict(server_options()),
    }


# ------------------------------------------------------------------ messages
class ProtocolError(ValueError):
    """The peer sent something that is not valid S2 protocol."""


def encode(msg: Message) -> bytes:
    body = json.dumps(msg, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large: {len(body)} bytes")
    return body


def decode(body: bytes) -> Message:
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large: {len(body)} bytes")
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"message is not JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("message is not a JSON object")
    return msg


def blob_size(value: object) -> int:
    """Validate a requested payload size (raises :class:`ProtocolError`)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProtocolError(f"bytes must be a number, got {value!r}")
    size = int(value)
    if not MIN_BLOB_BYTES <= size <= MAX_BLOB_BYTES:
        raise ProtocolError(
            f"bytes must be between {MIN_BLOB_BYTES} and {MAX_BLOB_BYTES}, got {size}"
        )
    return size


# ------------------------------------------------------------------ failures
class FailureType(StrEnum):
    """How a reachability attempt failed. The value is the diagnosis key."""

    CONNECTION_REFUSED = "connection_refused"
    CONNECT_TIMEOUT = "connect_timeout"
    TIMEOUT = "timeout"
    CONNECTION_RESET = "connection_reset"
    HOST_UNREACHABLE = "host_unreachable"
    STREAM_DROPPED = "stream_dropped"
    TLS_OR_HTTP2_MISMATCH = "tls_or_http2_mismatch"
    PROTOCOL_ERROR = "protocol_error"
    SERVER_ERROR = "server_error"
    MEASUREMENT_INVALID = "measurement_invalid"


#: The actionable half of the diagnosis: what a human should change.
FIX_HINT: Final[dict[FailureType, str]] = {
    FailureType.CONNECTION_REFUSED: (
        "the TCP port answered with RST: nothing is listening there. Check the "
        "server is running, that it bound this machine's lab IP (not 127.0.0.1), "
        "and that the port numbers match."
    ),
    FailureType.CONNECT_TIMEOUT: (
        "the SYN got no answer: a firewall is dropping it silently. On Windows "
        "check `Get-NetConnectionProfile` shows Private for the lab adapter and "
        "add an inbound allow rule for the spike ports scoped to the lab subnet "
        "(see lab/spikes/S2.md); on Linux check ufw/nftables."
    ),
    FailureType.TIMEOUT: (
        "connected, but the peer stopped answering in time: check the link (S1), "
        "raise --io-timeout, or look for a stateful firewall/NAT idle timeout on "
        "the path."
    ),
    FailureType.CONNECTION_RESET: (
        "the connection was reset: the server process died, a middlebox cut it, "
        "or something else is listening on that port."
    ),
    FailureType.HOST_UNREACHABLE: (
        "no route to the peer: wrong subnet, adapter down, or the laptops are on "
        "different networks (Wi-Fi vs Ethernet)."
    ),
    FailureType.STREAM_DROPPED: (
        "the long-lived stream was cut while idle: a stateful firewall/NAT "
        "dropped the idle flow. Re-run with --keepalive on; if that fixes it, "
        "the product's agent session must keep HTTP/2 keepalive enabled "
        "(ADR-0002)."
    ),
    FailureType.TLS_OR_HTTP2_MISMATCH: (
        "the port speaks a different protocol (plaintext gRPC dialed at a TLS "
        "port, or HTTP/2 at an HTTP/1 port). Check --grpc-port vs --http-port."
    ),
    FailureType.PROTOCOL_ERROR: "the peer is not running this spike's version; "
    "check both sides run the same commit.",
    FailureType.SERVER_ERROR: "the server rejected the request; read its stderr.",
    FailureType.MEASUREMENT_INVALID: "the measurement itself was not usable; "
    "see the message.",
}

#: After one of these the rest of the case is pointless: stop early.
FATAL: Final = frozenset(
    {
        FailureType.CONNECTION_REFUSED,
        FailureType.CONNECT_TIMEOUT,
        FailureType.HOST_UNREACHABLE,
    }
)


class ProbeError(Exception):
    """A reachability probe failed, with a typed diagnosis."""

    def __init__(
        self, failure_type: FailureType, message: str, *, detail: str = ""
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.detail = detail

    @property
    def fatal(self) -> bool:
        return self.failure_type in FATAL

    @property
    def fix(self) -> str:
        return FIX_HINT[self.failure_type]

    def to_json(self) -> Message:
        return {
            "failure_type": str(self.failure_type),
            "message": str(self),
            "detail": self.detail,
            "fix": self.fix,
            "fatal": self.fatal,
        }


_UNREACHABLE_ERRNOS: Final = frozenset(
    {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN}
)


def classify_os_error(
    exc: BaseException, doing: str, *, connecting: bool
) -> ProbeError:
    """Map a socket/http.client failure to a typed :class:`ProbeError`.

    ``connecting`` distinguishes "the SYN was dropped" (firewall) from "the peer
    went quiet mid-transfer", which have different fixes.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, ConnectionRefusedError):
        return ProbeError(
            FailureType.CONNECTION_REFUSED,
            f"{doing}: connection refused",
            detail=detail,
        )
    if isinstance(exc, TimeoutError):
        kind = FailureType.CONNECT_TIMEOUT if connecting else FailureType.TIMEOUT
        return ProbeError(kind, f"{doing}: timed out", detail=detail)
    if isinstance(exc, ConnectionResetError | BrokenPipeError | ConnectionAbortedError):
        kind = (
            FailureType.CONNECTION_RESET if connecting else FailureType.STREAM_DROPPED
        )
        return ProbeError(kind, f"{doing}: {exc}", detail=detail)
    if isinstance(exc, socket.gaierror) or (
        isinstance(exc, OSError) and exc.errno in _UNREACHABLE_ERRNOS
    ):
        return ProbeError(
            FailureType.HOST_UNREACHABLE, f"{doing}: {exc}", detail=detail
        )
    if isinstance(exc, OSError):
        kind = FailureType.CONNECT_TIMEOUT if connecting else FailureType.STREAM_DROPPED
        return ProbeError(kind, f"{doing}: {exc}", detail=detail)
    return ProbeError(FailureType.PROTOCOL_ERROR, f"{doing}: {exc}", detail=detail)


#: Substrings of a gRPC status detail -> diagnosis. Order matters: first match
#: wins, so the specific transport causes come before the generic ones.
_GRPC_MARKERS: Final[tuple[tuple[str, FailureType], ...]] = (
    ("connection refused", FailureType.CONNECTION_REFUSED),
    ("connect: connection refused", FailureType.CONNECTION_REFUSED),
    ("no route to host", FailureType.HOST_UNREACHABLE),
    ("network is unreachable", FailureType.HOST_UNREACHABLE),
    ("host is unreachable", FailureType.HOST_UNREACHABLE),
    ("wrong_version_number", FailureType.TLS_OR_HTTP2_MISMATCH),
    ("ssl", FailureType.TLS_OR_HTTP2_MISMATCH),
    ("handshake", FailureType.TLS_OR_HTTP2_MISMATCH),
    ("empty update", FailureType.TLS_OR_HTTP2_MISMATCH),
    ("connection reset by peer", FailureType.CONNECTION_RESET),
    ("connection reset", FailureType.CONNECTION_RESET),
    ("broken pipe", FailureType.CONNECTION_RESET),
    ("too_many_pings", FailureType.STREAM_DROPPED),
    ("keepalive watchdog", FailureType.STREAM_DROPPED),
    ("socket closed", FailureType.STREAM_DROPPED),
    ("endpoint read failed", FailureType.STREAM_DROPPED),
    ("goaway", FailureType.STREAM_DROPPED),
    ("timeout occurred", FailureType.CONNECT_TIMEOUT),
    ("timed out", FailureType.CONNECT_TIMEOUT),
)


def classify_grpc_error(
    code_name: str, details: str, debug: str, doing: str, *, connecting: bool
) -> ProbeError:
    """Map a gRPC status (code name + details + debug string) to a diagnosis.

    Kept free of ``grpc`` types so it is unit-testable from recorded strings.
    """
    text = f"{details} {debug}".lower()
    detail = f"{code_name}: {details or debug}".strip()
    for marker, kind in _GRPC_MARKERS:
        if marker in text:
            if kind is FailureType.CONNECT_TIMEOUT and not connecting:
                kind = FailureType.TIMEOUT
            if kind is FailureType.STREAM_DROPPED and connecting:
                kind = FailureType.CONNECTION_RESET
            return ProbeError(kind, f"{doing}: {code_name}: {details}", detail=detail)
    if code_name == "DEADLINE_EXCEEDED":
        kind = FailureType.CONNECT_TIMEOUT if connecting else FailureType.TIMEOUT
        return ProbeError(kind, f"{doing}: deadline exceeded", detail=detail)
    if code_name == "UNAVAILABLE":
        kind = FailureType.CONNECT_TIMEOUT if connecting else FailureType.STREAM_DROPPED
        return ProbeError(kind, f"{doing}: UNAVAILABLE: {details}", detail=detail)
    if code_name in {"UNIMPLEMENTED", "INVALID_ARGUMENT", "INTERNAL", "UNKNOWN"}:
        return ProbeError(
            FailureType.PROTOCOL_ERROR,
            f"{doing}: {code_name}: {details}",
            detail=detail,
        )
    return ProbeError(
        FailureType.SERVER_ERROR, f"{doing}: {code_name}: {details}", detail=detail
    )
