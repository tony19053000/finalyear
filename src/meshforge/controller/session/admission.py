"""Pure pieces of Hello admission (split out of ``manager.py``, D42).

What can be decided about a Hello without the session table or the registry:
the peer-controlled fields are bounded and printable before anything is kept
(:mod:`~.limits`, D12/D22 - a Hello over a cap is ``INVALID_ARGUMENT`` and nothing
is persisted), which §12.3 arrow an accepted Hello is, and the ``HelloAck``.
``SessionManager.admit`` applies these first; the epoch bound
(:data:`~.epoch.MAX_SESSION_EPOCH`), the protocol range and fencing stay in the
manager, which owns the configuration and the table.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from typing import Final

import grpc

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import new_command_id, validate_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, LivenessEvent, NodeState
from meshforge.controller.session.limits import check_hardware, check_hello_strings
from meshforge.controller.session.live import LiveSession, Rejected
from meshforge.controller.session.peer_cert import PeerCert
from meshforge.controller.store.models.node import MAX_SLOTS

__all__ = [
    "DEFAULT_ADMISSION_TIMEOUT_S",
    "MAX_HELLO_SLOTS",
    "MAX_PEER_ENDPOINT_CHARS",
    "AdmissionAttempt",
    "RejectCode",
    "check_hello",
    "hello_ack",
    "hello_event",
    "peer_endpoint_of",
    "shutting_down",
    "slots_of",
    "unavailable",
]

RejectCode = agent_pb2.HelloAck.RejectCode

DEFAULT_ADMISSION_TIMEOUT_S: Final = 5.0
"""D41: how long one admission (lock wait + registry round trips) may take."""


@dataclass(slots=True)
class AdmissionAttempt:
    """What one admission has done so far - read if it has to be abandoned (D41).

    ``row_written`` turns True right before ``upsert_node_on_hello`` is awaited:
    from then on the node row may point at ``session_id`` (the write may or may
    not have committed when the admission is cut short), so a failed admission
    must leave that row honest, not ONLINE with no session behind it.
    """

    hello: agent_pb2.Hello
    peer_address: str
    cert: PeerCert | None = None
    """V0.7 G3: the caller's verified client certificate (None in insecure-dev)."""
    session_id: str = ""
    state: NodeState | None = None
    """The state the admission wrote (or was writing) to the row."""
    superseded: LiveSession | None = None
    """The live session this admission closed as ``superseded``."""
    row_written: bool = False
    admitted: bool = False


def unavailable(timeout_s: float) -> Rejected:
    """The retryable refusal of a Hello whose admission missed its deadline."""
    return Rejected(
        RejectCode.REJECT_CODE_UNAVAILABLE,
        f"admission did not finish within {timeout_s:g} s (the controller's "
        "database is slow or unreachable); retry",
    )


def shutting_down() -> Rejected:
    """The refusal of a Hello that arrives while the controller shuts down."""
    return Rejected(
        RejectCode.REJECT_CODE_INTERNAL,
        "controller is shutting down",
        grpc.StatusCode.UNAVAILABLE,
    )


def check_hello(hello: agent_pb2.Hello) -> Rejected | None:
    """Refuse a Hello whose ``node_id``, strings or hardware break a bound."""
    # V0.7 G3 (D137): in mtls mode the servicer has already checked that
    # ``Hello.node_id`` equals the client certificate's node (the transport is
    # the identity). Only ``[security] mode = "insecure-dev"`` (loopback only)
    # still takes the field as a claim.
    try:
        validate_node_id(hello.node_id)
    except InvalidIdError as exc:
        return Rejected(
            RejectCode.REJECT_CODE_INTERNAL, str(exc), grpc.StatusCode.INVALID_ARGUMENT
        )
    too_long = check_hello_strings(
        hostname=hello.hostname,
        os=hello.os,
        platform=hello.platform,
        agent_version=hello.agent_version,
    )
    if too_long is not None:
        return Rejected(
            RejectCode.REJECT_CODE_INTERNAL, too_long, grpc.StatusCode.INVALID_ARGUMENT
        )
    # D12: the hardware summary is part of the enrolment contract - over a cap
    # the Hello is refused and nothing is persisted. The agent clips to the
    # same numbers before sending, so this only fires on a hostile or broken
    # peer (roadmap 0.3.3, G2 review follow-up on unbounded `hardware` JSON).
    oversized = check_hardware(hello.hardware)
    if oversized is not None:
        return Rejected(
            RejectCode.REJECT_CODE_INTERNAL, oversized, grpc.StatusCode.INVALID_ARGUMENT
        )
    return None


def hello_event(prior: NodeState) -> LivenessEvent | None:
    """Which §12.3 arrow an accepted Hello is, given the node's previous state."""
    if prior is NodeState.ENROLLING:
        return LivenessEvent.HELLO_ACCEPTED
    if prior in (NodeState.SUSPECT, NodeState.OFFLINE):
        return LivenessEvent.RECONNECTED
    if prior is NodeState.ONLINE:
        return LivenessEvent.HEARTBEAT  # superseding stream: self-loop
    return None  # DRAINING keeps draining; REVOKED is rejected before this


def hello_ack(
    session_id: str,
    liveness: LivenessConfig,
    now: dt.datetime,
    peer_token_key: bytes = b"",
) -> agent_pb2.ControllerMessage:
    """The accepting ``HelloAck``: the session id, the controller-owned timing
    and the public key that signs peer blob tokens (V0.5 D98)."""
    ack = agent_pb2.HelloAck(
        accepted=True,
        session_id=session_id,
        heartbeat_interval_ms=round(liveness.heartbeat_interval_s * 1000),
        lease_ms=round(liveness.offline_after_s * 1000),
        peer_token_key=peer_token_key,
    )
    ack.controller_time.FromDatetime(now)
    return agent_pb2.ControllerMessage(message_id=new_command_id(), hello_ack=ack)


MAX_PEER_ENDPOINT_CHARS: Final = 64
MAX_HELLO_SLOTS: Final = MAX_SLOTS
"""Largest ``Hello.max_cpu_jobs`` / ``max_gpu_jobs`` accepted (V0.6 D105)."""
"""``[ipv6]:65535`` fits; anything longer is not an endpoint."""


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _split_host_port(text: str) -> tuple[str, int] | None:
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not rest.startswith(":"):
            return None
        port_text = rest[1:]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep or ":" in host:
            return None
    if not port_text.isdigit() or len(port_text) > 5:
        return None
    return host, int(port_text)


def _transport_host(peer_address: str) -> str | None:
    """The IP of a gRPC peer string (``ipv4:1.2.3.4:5``, ``ipv6:[::1]:5``)."""
    scheme, sep, rest = peer_address.partition(":")
    if not sep or scheme not in ("ipv4", "ipv6"):
        return None
    split = _split_host_port(rest)
    return None if split is None else split[0]


def slots_of(hello: agent_pb2.Hello) -> tuple[int | None, int | None, str | None]:
    """V0.6 D105: ``(max_cpu_jobs, max_gpu_jobs, refusal)``. A field the agent
    did not send is None (an older agent - the planner's defaults apply); a value
    above :data:`MAX_HELLO_SLOTS` is ignored (None, ``refusal`` names it), never
    an admission failure."""
    cpu = hello.max_cpu_jobs if hello.HasField("max_cpu_jobs") else None
    gpu = hello.max_gpu_jobs if hello.HasField("max_gpu_jobs") else None
    refused = []
    if cpu is not None and cpu > MAX_HELLO_SLOTS:
        cpu = None
        refused.append("max_cpu_jobs")
    if gpu is not None and gpu > MAX_HELLO_SLOTS:
        gpu = None
        refused.append("max_gpu_jobs")
    return cpu, gpu, ",".join(refused) or None


def peer_endpoint_of(
    hello: agent_pb2.Hello, peer_address: str, *, trust: bool
) -> tuple[str, str | None]:
    """D99: ``(endpoint, refusal)`` for ``Hello.peer_endpoint``.

    Accepted (``refusal`` None) only when it is ``host:port`` with an IP host,
    1 <= port <= 65535 and - unless ``trust`` (``[controller]
    trust_peer_endpoints``) - the host is the address the session comes from.
    A refused endpoint is ignored (the node simply serves no peers), never an
    admission failure. Returns the normalised ``host:port``.
    """
    raw = hello.peer_endpoint
    if not raw:
        return "", None
    if len(raw) > MAX_PEER_ENDPOINT_CHARS or not raw.isprintable():
        return "", "malformed"
    split = _split_host_port(raw)
    if split is None:
        return "", "malformed"
    host_text, port = split
    host = _ip(host_text)
    if host is None or host.is_unspecified or host.is_multicast:
        return "", "bad_host"
    if not 1 <= port <= 65535:
        return "", "bad_port"
    if not trust:
        seen = _transport_host(peer_address)
        seen_ip = None if seen is None else _ip(seen)
        if seen_ip is None or seen_ip != host:
            return "", "host_mismatch"
    shown = f"[{host}]" if host.version == 6 else str(host)
    return f"{shown}:{port}", None
