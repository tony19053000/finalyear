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
from dataclasses import dataclass
from typing import Final

import grpc

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import new_command_id, validate_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, LivenessEvent, NodeState
from meshforge.controller.session.limits import check_hardware, check_hello_strings
from meshforge.controller.session.live import LiveSession, Rejected

__all__ = [
    "DEFAULT_ADMISSION_TIMEOUT_S",
    "AdmissionAttempt",
    "RejectCode",
    "check_hello",
    "hello_ack",
    "hello_event",
    "shutting_down",
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
    # TRUST POINT (until V0.7 mTLS, ADR-0008): ``Hello.node_id`` is a claim
    # accepted on the trusted LAN (ADR-0013 §2). Pairing binds node_id -> key
    # -> certificate; then the transport, not this field, proves identity.
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
    session_id: str, liveness: LivenessConfig, now: dt.datetime
) -> agent_pb2.ControllerMessage:
    """The accepting ``HelloAck``: the session id and the controller-owned timing."""
    ack = agent_pb2.HelloAck(
        accepted=True,
        session_id=session_id,
        heartbeat_interval_ms=round(liveness.heartbeat_interval_s * 1000),
        lease_ms=round(liveness.offline_after_s * 1000),
    )
    ack.controller_time.FromDatetime(now)
    return agent_pb2.ControllerMessage(message_id=new_command_id(), hello_ack=ack)
