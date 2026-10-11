"""The interim TLS enrolment listener (V0.7 G2, D150) and its per-IP rate limit.

G2 keeps the session port plaintext, so ``Enroll`` gets a listener of its own:

* **TLS** with the controller's server certificate; the chain it presents is
  ``server.crt`` **+** ``ca.crt``, so a joining agent can read the CA from the
  handshake (with verification off) and check it against its code's pin before
  it sends anything secret (D136). No client certificate is requested.
* serves exactly one service (``meshforge.v1.Enrollment``); requests are capped at
  :data:`MAX_REQUEST_BYTES` and :data:`MAX_CONCURRENT_RPCS` run at a time;
* every call is first charged to its **source** (:func:`rate_key`: the IPv4
  address, the IPv6 /64, or one shared ``unknown`` bucket; :class:`SourceRateLimiter`,
  a token bucket, ``[controller.enroll] rate_per_minute`` / ``rate_burst``); an
  empty bucket is ``RESOURCE_EXHAUSTED`` before the database is touched, and it
  never affects another IP.

Every refusal is a gRPC status plus trailing metadata ``meshforge-reason``
(:class:`~meshforge.controller.enrollment.service.EnrollReason` or
``csr_<pki reason>``). G3 folds ``Enroll`` into the session port and removes this
listener.
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import re
import urllib.parse
from collections.abc import Callable
from typing import Final

import grpc

from meshforge.common.errors import ConfigError
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import BindAddress
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.enrollment.service import (
    Enroller,
    EnrollReason,
    EnrollRefused,
)

__all__ = [
    "CONNECTION_OPTIONS",
    "ENROLL_DEADLINE_S",
    "MAX_CONCURRENT_RPCS",
    "MAX_REQUEST_BYTES",
    "REASON_METADATA_KEY",
    "UNKNOWN_SOURCE",
    "EnrollListener",
    "EnrollmentServicer",
    "SourceRateLimiter",
    "ip_rate_key",
    "rate_key",
    "source_ip",
    "status_for",
]

log = get_logger(__name__)

REASON_METADATA_KEY: Final = "meshforge-reason"
MAX_REQUEST_BYTES: Final = 16 * 1024
"""A 16-byte token + a <= 4096-byte CSR + framing; anything bigger is refused."""
MAX_CONCURRENT_RPCS: Final = 16
ENROLL_DEADLINE_S: Final = 10.0
"""Server-side bound on one ``Enroll`` (D153 item 8): past it the call is
refused ``UNAVAILABLE`` / ``unavailable`` and its transaction rolled back, so
the code is not spent (whatever deadline the client set, or none)."""
CONNECTION_OPTIONS: Final[tuple[tuple[str, int], ...]] = (
    ("grpc.max_connection_idle_ms", 30_000),
    ("grpc.max_connection_age_ms", 120_000),
    ("grpc.max_connection_age_grace_ms", 15_000),
)
"""D153 item 8: an idle connection is closed after 30 s and any connection
after 2 min (+15 s grace for a call in flight), so unauthenticated clients
cannot hold the listener's sockets open."""
_PEER_RE: Final = re.compile(
    r"ipv4:(?P<v4>[0-9.]+):\d+|ipv6:\[(?P<v6>[0-9a-zA-Z:.%_-]+)\]:\d+"
)

_STATUS: Final[dict[str, grpc.StatusCode]] = {
    EnrollReason.MALFORMED_REQUEST: grpc.StatusCode.INVALID_ARGUMENT,
    EnrollReason.TOKEN_UNKNOWN: grpc.StatusCode.UNAUTHENTICATED,
    EnrollReason.TOKEN_USED: grpc.StatusCode.UNAUTHENTICATED,
    EnrollReason.TOKEN_REVOKED: grpc.StatusCode.UNAUTHENTICATED,
    EnrollReason.TOKEN_EXPIRED: grpc.StatusCode.UNAUTHENTICATED,
    EnrollReason.REPLACE_MISMATCH: grpc.StatusCode.FAILED_PRECONDITION,
    EnrollReason.NODE_KEY_CONFLICT: grpc.StatusCode.ALREADY_EXISTS,
    EnrollReason.NODE_REVOKED: grpc.StatusCode.PERMISSION_DENIED,
    EnrollReason.NO_FABRIC: grpc.StatusCode.FAILED_PRECONDITION,
    EnrollReason.RATE_LIMITED: grpc.StatusCode.RESOURCE_EXHAUSTED,
    EnrollReason.UNAVAILABLE: grpc.StatusCode.UNAVAILABLE,
    EnrollReason.INTERNAL: grpc.StatusCode.INTERNAL,
}


def status_for(reason: str) -> grpc.StatusCode:
    """The gRPC status of a refusal reason (``csr_*`` -> ``INVALID_ARGUMENT``)."""
    if reason.startswith("csr_"):
        return grpc.StatusCode.INVALID_ARGUMENT
    return _STATUS.get(reason, grpc.StatusCode.INTERNAL)


def source_ip(peer: str) -> str:
    """The IP of a gRPC peer string, for logs and audit.

    gRPC percent-encodes the brackets of an IPv6 peer (``ipv6:%5B::1%5D:5123``),
    so the string is URL-decoded first (D152). Anything unparseable (a unix
    socket) is returned as is, bounded to 128 characters.
    """
    decoded = urllib.parse.unquote(peer)
    match = _PEER_RE.fullmatch(decoded)
    if match is None:
        return decoded[:128]
    return match["v4"] or match["v6"]


UNKNOWN_SOURCE: Final = "unknown"
"""The one bucket every unparseable peer shares (D152: fail closed - a peer the
limiter cannot place never gets a fresh bucket of its own)."""
IPV6_PREFIX: Final = 64


def rate_key(peer: str) -> str:
    """The rate-limit bucket of a gRPC peer (D152).

    IPv4: the address. IPv6: its **/64** - one laptop can rotate through a whole
    /64 of privacy addresses, so per-address buckets would not limit it. An
    IPv4-mapped IPv6 address counts as the IPv4 address; a zone id is dropped.
    Anything that does not parse as either: :data:`UNKNOWN_SOURCE`.
    """
    match = _PEER_RE.fullmatch(urllib.parse.unquote(peer))
    if match is None:
        return UNKNOWN_SOURCE
    return ip_rate_key(match["v4"] or match["v6"])


def ip_rate_key(host: str | None) -> str:
    """:func:`rate_key` for a bare IP address (an HTTP client's host, V0.7 G4
    login backoff): the IPv4 address, the IPv6 /64, else :data:`UNKNOWN_SOURCE`."""
    if not host:
        return UNKNOWN_SOURCE
    text = host.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return UNKNOWN_SOURCE
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, IPV6_PREFIX), strict=False))
    return str(ip)


class SourceRateLimiter:
    """Token bucket per source key; the clock is monotonic (``loop.time``).

    ``rate_per_minute`` refills, ``burst`` is the bucket size. At most
    ``max_sources`` keys are remembered (least recently charged forgotten first:
    a forgotten source starts again with a full bucket - a bounded table cannot
    do better, and the database is protected by ``MAX_CONCURRENT_RPCS`` too).
    """

    def __init__(
        self,
        *,
        rate_per_minute: float,
        burst: int,
        max_sources: int,
        clock: Callable[[], float],
    ) -> None:
        if rate_per_minute <= 0 or burst < 1 or max_sources < 1:
            raise ValueError("rate, burst and max_sources must be positive")
        self._rate_s = rate_per_minute / 60.0
        self._burst = float(burst)
        self._max = max_sources
        self._clock = clock
        self._buckets: collections.OrderedDict[str, tuple[float, float]] = (
            collections.OrderedDict()
        )

    def allow(self, source: str) -> bool:
        """Charge one attempt to ``source``; ``False`` when its bucket is empty."""
        now = self._clock()
        tokens, last = self._buckets.pop(source, (self._burst, now))
        tokens = min(self._burst, tokens + max(0.0, now - last) * self._rate_s)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[source] = (tokens, now)
        while len(self._buckets) > self._max:
            self._buckets.popitem(last=False)
        return allowed

    def __len__(self) -> int:
        return len(self._buckets)


class EnrollmentServicer(enroll_pb2_grpc.EnrollmentServicer):
    """``Enroll``: rate limit -> :meth:`Enroller.enroll` -> typed status."""

    def __init__(
        self,
        enroller: Enroller,
        limiter: SourceRateLimiter,
        *,
        deadline_s: float = ENROLL_DEADLINE_S,
    ) -> None:
        self._enroller = enroller
        self._limiter = limiter
        self._deadline_s = deadline_s

    async def Enroll(  # noqa: N802 - gRPC method name
        self,
        request: enroll_pb2.EnrollRequest,
        context: grpc.aio.ServicerContext[
            enroll_pb2.EnrollRequest, enroll_pb2.EnrollResponse
        ],
    ) -> enroll_pb2.EnrollResponse:
        peer = context.peer() or ""
        ip = source_ip(peer)
        if not self._limiter.allow(rate_key(peer)):
            log.warning("controller.enroll.rate_limited", source=ip)
            await _refuse(
                context,
                EnrollReason.RATE_LIMITED,
                "too many enrolment attempts from this address; wait a minute",
            )
        try:
            result = await asyncio.wait_for(
                self._enroller.enroll(
                    bytes(request.join_token), bytes(request.csr_pem), source=peer
                ),
                self._deadline_s,
            )
        except TimeoutError:
            log.warning(
                "controller.enroll.deadline", source=ip, deadline_s=self._deadline_s
            )
            await _refuse(
                context,
                EnrollReason.UNAVAILABLE,
                "the enrolment took too long and was abandoned (the code was not "
                "used); retry",
            )
            raise AssertionError("unreachable: abort raises") from None
        except EnrollRefused as refused:
            log.warning(
                "controller.enroll.refused",
                reason=str(refused.reason),
                node_id=refused.node_id,
                invite_id=refused.invite_id,
                source=ip,
            )
            await _refuse(context, str(refused.reason), refused.message)
        return enroll_pb2.EnrollResponse(
            node_cert_pem=result.node_cert_pem,
            ca_cert_pem=result.ca_cert_pem,
            fabric_id=result.fabric_id,
            node_id=result.node_id,
        )


async def _refuse(
    context: grpc.aio.ServicerContext[
        enroll_pb2.EnrollRequest, enroll_pb2.EnrollResponse
    ],
    reason: str,
    message: str,
) -> None:
    context.set_trailing_metadata(((REASON_METADATA_KEY, reason),))
    await context.abort(status_for(reason), message)


class EnrollListener:
    """Owns the TLS ``grpc.aio`` server that serves :class:`EnrollmentServicer`."""

    def __init__(self, servicer: EnrollmentServicer) -> None:
        self._servicer = servicer
        self._server: grpc.aio.Server | None = None
        self.port = 0

    async def start(
        self, bind: BindAddress, credentials: grpc.ServerCredentials
    ) -> int:
        """Listen on ``bind`` (port 0 = any) with ``credentials`` (the
        controller's :meth:`~meshforge.controller.server_tls.ServerTls.grpc_credentials`,
        no client certificate); returns the bound port."""
        server = grpc.aio.server(
            options=[
                ("grpc.max_receive_message_length", MAX_REQUEST_BYTES),
                ("grpc.max_send_message_length", 64 * 1024),
                *CONNECTION_OPTIONS,
            ],
            maximum_concurrent_rpcs=MAX_CONCURRENT_RPCS,
        )
        enroll_pb2_grpc.add_EnrollmentServicer_to_server(self._servicer, server)
        port = server.add_secure_port(bind.grpc_target(), credentials)
        if port == 0:
            raise ConfigError(
                f"cannot bind the enrolment listener on {bind.grpc_target()}",
                details={"bind": bind.grpc_target()},
            )
        await server.start()
        self._server, self.port = server, port
        return port

    async def stop(self, grace: float) -> None:
        if self._server is not None:
            await self._server.stop(grace=grace)
            self._server = None
