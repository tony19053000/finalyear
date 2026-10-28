"""The in-memory model of one session (split out of ``manager.py``, V0.4 G1 / D42).

:class:`LiveSession` is the state of one accepted stream, owned by
:class:`~meshforge.controller.session.manager.SessionManager`; the admission
results (:class:`Accepted`, :class:`Rejected`) and :func:`end_event` - which
§12.3 event a session end fires - live here so the manager, the claims, the
report handlers and the lease timer share one definition without importing each
other. ``manager`` re-exports every public name for its existing importers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from typing import Final

import grpc

from meshforge.common.owner_policy import OwnerPolicy
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LeaseTracker, LivenessEvent, NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.clocks import NodeClock
from meshforge.controller.session.writer import WriteChain

__all__ = [
    "IDLE_SOURCES",
    "MAX_AWAITING_ACK",
    "OWNER_REASONS",
    "RTT_EWMA_ALPHA",
    "Accepted",
    "AdmitResult",
    "LiveSession",
    "PendingCommand",
    "Rejected",
    "SeenIds",
    "end_event",
    "update_ewma",
]

RTT_EWMA_ALPHA: Final = 0.3
"""Weight of the newest RTT sample in ``rtt_ewma_ms`` (roadmap 0.3.10)."""
MAX_AWAITING_ACK: Final = 4096
"""Commands remembered as "sent, not yet acked" per session (D64); the oldest is
forgotten above this - its Ack then only logs. Commands are rare (one per
attempt start/cancel), so this bounds a broken agent, not normal use."""


@dataclass(slots=True)
class PendingCommand:
    """A controller command waiting for the agent's Ack (D64).

    V0.6 G1 (D113): a command re-sent on a missing Ack keeps its ``message``
    (the exact envelope, same ``message_id``: the agent's dedupe drops a copy
    it already applied) and when it was last sent; attempt commands keep no
    message - Reconcile re-sends what still matters for them."""

    payload: str
    attempt_id: str = ""
    fencing_token: int = 0
    message: agent_pb2.ControllerMessage | None = None
    sent_at: float = 0.0
    sends: int = 1


_CLEAN_END: Final[frozenset[SessionEndReason]] = frozenset(
    {
        SessionEndReason.AGENT_CLOSED,
        SessionEndReason.CONTROLLER_SHUTDOWN,
        # V0.7 G3 fix r1 (D155): the stream's certificate can never be used
        # again, so nothing is gained by lingering - OFFLINE now, and the
        # node's attempts take the node-loss path at once.
        SessionEndReason.CERT_SUPERSEDED,
        SessionEndReason.CERT_EXPIRED,
    }
)
_ABRUPT_END: Final[frozenset[SessionEndReason]] = frozenset(
    {
        SessionEndReason.STREAM_LOST,
        SessionEndReason.PROTOCOL_ERROR,
        SessionEndReason.INTERNAL_ERROR,
    }
)


def end_event(reason: SessionEndReason) -> LivenessEvent | None:
    """The §12.3 event a session end fires (D19); None = no state write.

    ``superseded`` fires nothing (the new session's Hello owns the state),
    ``lease_expired`` nothing extra (the timer already applied the expiry),
    ``revoked`` nothing extra (the revocation applied ``REVOKE`` before it cut
    the stream, V0.7 G3), ``cert_superseded`` / ``cert_expired`` the clean
    arrow (OFFLINE now: that certificate never comes back, D155) and
    ``controller_restart`` is only ever written by the start-up sweep.
    """
    if reason in _CLEAN_END:
        return LivenessEvent.STREAM_CLOSED
    if reason in _ABRUPT_END:
        return LivenessEvent.STREAM_LOST
    return None


def update_ewma(
    previous: float | None, sample: float, alpha: float = RTT_EWMA_ALPHA
) -> float:
    """Exponentially weighted moving average; the first sample seeds it."""
    if previous is None:
        return sample
    return alpha * sample + (1.0 - alpha) * previous


class SeenIds:
    """Bounded ``message_id`` window (deque for order + set for O(1) lookup)."""

    __slots__ = ("_order", "_set")

    def __init__(self, capacity: int) -> None:
        self._order: deque[str] = deque(maxlen=capacity)
        self._set: set[str] = set()

    def remember(self, message_id: str) -> bool:
        """Record ``message_id``; return True if it was already present."""
        if message_id in self._set:
            return True
        if self._order.maxlen is not None and len(self._order) == self._order.maxlen:
            self._set.discard(self._order[0])
        self._order.append(message_id)
        self._set.add(message_id)
        return False

    def __len__(self) -> int:
        return len(self._set)


OWNER_REASONS: Final = frozenset({"", "idle", "cpu", "idle+cpu"})
"""What a heartbeat's ``owner_active_reason`` may say (anything else is "")."""
IDLE_SOURCES: Final = frozenset({"", "logind", "windows", "unknown"})


@dataclass(eq=False, slots=True)
class LiveSession:
    """In-memory state of one accepted stream. Owned by the ``SessionManager``."""

    node_id: str
    session_id: str
    session_epoch: int
    peer_address: str
    tracker: LeaseTracker
    state: NodeState
    outbound: asyncio.Queue[agent_pb2.ControllerMessage | None]
    seen: SeenIds
    next_ping_at: float
    writer: WriteChain
    """This session's ordered registry-write chain (see ``manager``'s docstring)."""
    reader_task: asyncio.Task[None] | None = None
    ended: bool = False
    end_reason: SessionEndReason | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lingering: bool = False
    """The stream ended abruptly and the lease is still running (D19): the entry
    sits in ``SessionManager._lingering`` until a Hello, the lease or a shutdown
    resolves it. Its ``writer`` stays open until then."""
    fence_ids: tuple[str, ...] = ()
    """Other session ids this entry's state writes are also fenced on (D41): an
    abandoned admission cannot know whether its row write committed, so it writes
    for its own id *and* for the sessions the row may still name; at most one
    matches."""
    open_uncertain: bool = False
    """An abandoned admission's entry (D41): its ``open_session`` may never have
    committed, so its ``close_session`` finding no row is expected, not a failure
    (V1.0 G4 F-1)."""
    # counters
    heartbeats: int = 0
    stale_messages: int = 0
    duplicates: int = 0
    resource_updates: int = 0
    resource_updates_clamped: int = 0
    """Reports that broke a D12 cap; logged once, counted always."""
    agent_version: str = ""
    """V0.17 G5: ``Hello.agent_version`` (already checked against Q37); the
    Nodes page shows it and marks an agent one minor behind."""
    peer_endpoint: str = ""
    """V0.5 D99: validated ``host:port`` of the node's peer blob server ("" =
    none or refused). Only live (ONLINE) sessions are listed as blob sources."""
    cert_serial: str | None = None
    """V0.7 G3 fix r1 (D155 T-1): serial of the client certificate this stream
    was admitted with (None: ``insecure-dev``); the revocation sync cuts the
    stream when the serial joins the revocation set."""
    cert_expiry: asyncio.TimerHandle | None = None
    """V0.7 G3 fix r1 (D155 T-3): fires at the certificate's ``not_after`` and
    cuts the stream ``cert_expired``; cancelled when the session closes."""
    cert_not_after: dt.datetime | None = None
    """The admitted certificate's ``not_after`` (wall clock): the expiry timer
    re-checks it when it fires (D156 d)."""
    policy: OwnerPolicy | None = None
    """V0.7 G5 (D142): the owner policy the node advertised (Hello, then each
    PolicyUpdate); None = an agent from before G5 (:mod:`.policies`)."""
    policy_refused: str | None = None
    """Why the last advertised policy was refused (the session then holds
    ``DENY_ALL``, fail closed); None = valid or absent."""
    policy_slots: tuple[int | None, int | None] | None = None
    """Slots from the newest PolicyUpdate (override the Hello's); None = none
    received in this session."""
    owner_active: bool = False
    """V0.9 G5 (D219/D246): the newest heartbeat says the node's owner is using
    it (benchmark jobs wait; user jobs are not affected)."""
    owner_active_reason: str = ""
    """``idle`` / ``cpu`` / "" - bounded to :data:`OWNER_REASONS`."""
    idle_source: str = ""
    """``logind`` / ``windows`` / ``unknown`` / "" - bounded to :data:`IDLE_SOURCES`."""
    declared_gpus: frozenset[int] = frozenset()
    """``Hello.hardware.gpus[].index`` of this session: the only GPU indices its
    score reports may name (D39)."""
    hardware_scores_received: int = 0
    hardware_scores_rejected: int = 0
    """Score sets (Hello or report) dropped by D38 validation; each is logged."""
    # RTT (roadmap 0.3.10) - monotonic clock only
    outstanding_ping: tuple[str, float] | None = None
    outstanding_ping_wall_ms: float | None = None
    """The controller's WALL clock (epoch ms) when the outstanding Ping was
    queued: the ``sent`` of its clock-offset exchange (V0.9 G1, D213)."""
    clock: NodeClock = field(default_factory=NodeClock)
    """This node's clock offset from Ping/Pong exchanges (:mod:`.clocks`)."""
    last_rtt_ms: float | None = None
    rtt_ewma_ms: float | None = None
    pings_sent: int = 0
    pongs_matched: int = 0
    # coalesced ``last_seen_at`` persistence (monotonic clock only)
    last_touch_at: float | None = None
    """When the last ``touch_node`` write was issued; None before the first."""
    touch_dirty: bool = False
    """Heartbeats arrived since ``last_touch_at`` that are not yet persisted."""
    # commands and attempt reports (V0.4 G4, D64/D52)
    awaiting_ack: dict[str, PendingCommand] = field(default_factory=dict)
    """Commands sent on this session and not yet acked, by ``message_id``."""
    commands_sent: int = 0
    commands_acked: int = 0
    commands_resent: int = 0
    """Commands queued again on a missing Ack (V0.6 G1, D113)."""
    attempt_status_received: int = 0
    attempt_status_fenced: int = 0
    """Reports for a stale token / an attempt the controller already ended."""
    attempt_status_wrong_node: int = 0
    """V0.8 G1 (D168 N1): reports about an attempt placed on another node
    (THREAT_MODEL CG-E2) - a security refusal, never counted as fencing."""
    attempt_status_rejected: int = 0
    """Reports that broke a bound (dropped, logged)."""
    attempt_status_dropped: int = 0
    """Reports not queued because the status queue was full (not acked: replayed)."""

    def summary(self) -> dict[str, int | float | None]:
        """Every per-session counter, for the ``session.closed`` log line (D43)."""
        return {
            "heartbeats": self.heartbeats,
            "stale_messages": self.stale_messages,
            "duplicates": self.duplicates,
            "resource_updates": self.resource_updates,
            "resource_updates_clamped": self.resource_updates_clamped,
            "hardware_scores_received": self.hardware_scores_received,
            "hardware_scores_rejected": self.hardware_scores_rejected,
            "pings_sent": self.pings_sent,
            "pongs_matched": self.pongs_matched,
            "last_rtt_ms": None
            if self.last_rtt_ms is None
            else round(self.last_rtt_ms, 3),
            "clock_offset_ms": None
            if self.clock.latest is None
            else round(self.clock.latest.offset_ms, 3),
            "clock_exchanges_rejected": self.clock.exchanges_rejected,
            "pending_writes": self.writer.pending,
            "writes_failed": self.writer.failed,
            "writes_dropped": self.writer.dropped,
            "write_overflow_episodes": self.writer.overflow_episodes,
            "commands_sent": self.commands_sent,
            "commands_acked": self.commands_acked,
            "commands_resent": self.commands_resent,
            "commands_unacked": len(self.awaiting_ack),
            "attempt_status_received": self.attempt_status_received,
            "attempt_status_fenced": self.attempt_status_fenced,
            "attempt_status_wrong_node": self.attempt_status_wrong_node,
            "attempt_status_rejected": self.attempt_status_rejected,
            "attempt_status_dropped": self.attempt_status_dropped,
        }

    def touch_due_at(self, interval_s: float) -> float | None:
        """Monotonic time the pending ``last_seen_at`` write is due (None if clean)."""
        if not self.touch_dirty or self.last_touch_at is None:
            return None
        return self.last_touch_at + interval_s


@dataclass(frozen=True, slots=True)
class Accepted:
    live: LiveSession
    hello_ack: agent_pb2.ControllerMessage


@dataclass(frozen=True, slots=True)
class Rejected:
    """Hello refused. ``abort_status`` set = close with that gRPC status instead of a
    HelloAck (malformed/forbidden Hello); otherwise a ``HelloAck{accepted=false}``."""

    reject_code: agent_pb2.HelloAck.RejectCode.ValueType
    reason: str
    abort_status: grpc.StatusCode | None = None
    reason_code: str | None = None
    """V0.7 G3: sent as trailing metadata ``meshforge-reason`` with an abort."""


type AdmitResult = Accepted | Rejected
