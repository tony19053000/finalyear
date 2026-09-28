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
from collections import deque
from dataclasses import dataclass, field
from typing import Final

import grpc

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LeaseTracker, LivenessEvent, NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.writer import WriteChain

__all__ = [
    "RTT_EWMA_ALPHA",
    "Accepted",
    "AdmitResult",
    "LiveSession",
    "Rejected",
    "SeenIds",
    "end_event",
    "update_ewma",
]

RTT_EWMA_ALPHA: Final = 0.3
"""Weight of the newest RTT sample in ``rtt_ewma_ms`` (roadmap 0.3.10)."""

_CLEAN_END: Final[frozenset[SessionEndReason]] = frozenset(
    {SessionEndReason.AGENT_CLOSED, SessionEndReason.CONTROLLER_SHUTDOWN}
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
    ``lease_expired`` nothing extra (the timer already applied the expiry) and
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
    # counters
    heartbeats: int = 0
    stale_messages: int = 0
    duplicates: int = 0
    resource_updates: int = 0
    resource_updates_clamped: int = 0
    """Reports that broke a D12 cap; logged once, counted always."""
    declared_gpus: frozenset[int] = frozenset()
    """``Hello.hardware.gpus[].index`` of this session: the only GPU indices its
    score reports may name (D39)."""
    hardware_scores_received: int = 0
    hardware_scores_rejected: int = 0
    """Score sets (Hello or report) dropped by D38 validation; each is logged."""
    # RTT (roadmap 0.3.10) - monotonic clock only
    outstanding_ping: tuple[str, float] | None = None
    last_rtt_ms: float | None = None
    rtt_ewma_ms: float | None = None
    pings_sent: int = 0
    pongs_matched: int = 0
    # coalesced ``last_seen_at`` persistence (monotonic clock only)
    last_touch_at: float | None = None
    """When the last ``touch_node`` write was issued; None before the first."""
    touch_dirty: bool = False
    """Heartbeats arrived since ``last_touch_at`` that are not yet persisted."""

    def summary(self) -> dict[str, int | float | None]:
        """The session's counters for the ``session.closed`` log line."""
        return {
            "heartbeats": self.heartbeats,
            "stale_messages": self.stale_messages,
            "duplicates": self.duplicates,
            "pending_writes": self.writer.pending,
            "last_rtt_ms": None
            if self.last_rtt_ms is None
            else round(self.last_rtt_ms, 3),
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


type AdmitResult = Accepted | Rejected
