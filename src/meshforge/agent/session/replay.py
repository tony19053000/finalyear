"""Bounded replay ring for un-acked *reports* (ADR-0002 Updates §4) and the
controller-command dedupe window.

Only reports are buffered - ``ResourceUpdate``, ``HardwareScores``,
``AttemptStatus``, ``LogChunk``, ``CacheReport``, ``NetProbeResult``: messages
whose loss would lose information the controller cannot recover otherwise.
**Heartbeats, Pongs, Acks, Hello and Reconcile are never buffered**: a missed
heartbeat is information in itself, and the others are per-stream.

Each report gets a monotonic ``sequence`` (per agent process, never reset across
reconnects) so ``Reconcile.replay_from_sequence`` can tell the controller where the
replay starts. The sequence is shared by every report kind; only ``ResourceUpdate``
also carries it in its payload, so the controller's resource dedupe cursor sees
gaps where other reports were sent - gaps it tolerates by design (the cursor only
ever compares ``<=``; pinned by
``tests/component/agent/test_hardware_scores.py``). On overflow the *oldest*
entry is dropped and ``dropped`` counts it - the controller learns about the gap
from the sequence numbers, and the agent logs ``agent.session.replay_dropped``.

Ordering is by insertion (= by sequence); an ``Ack`` removes by ``message_id``
wherever the entry sits (acks may arrive out of order after a replay).
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Final

from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = ["REPORT_PAYLOADS", "RecentIds", "ReplayEntry", "ReplayRing"]

REPORT_PAYLOADS: Final[frozenset[str]] = frozenset(
    {
        "resource_update",
        "hardware_scores",
        "cache_report",
        "net_probe_result",
        "attempt_status",
        "log_chunk",
        "prepare_env_status",  # V0.5 G5 (0.5.8)
        "policy_update",  # V0.7 G5 (0.7.8, D142)
    }
)
"""``AgentMessage`` payload names that are reports (buffered + replayed)."""


@dataclass(frozen=True, slots=True)
class ReplayEntry:
    sequence: int
    message: agent_pb2.AgentMessage
    """The envelope as first sent; ``session_id`` is rewritten at replay time."""


class ReplayRing:
    """Insertion-ordered, bounded, ack-removable buffer of reports."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._entries: OrderedDict[str, ReplayEntry] = OrderedDict()
        self.dropped = 0
        """Entries evicted because the ring was full (``session.replay_dropped``)."""

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._entries)

    def append(self, sequence: int, message: agent_pb2.AgentMessage) -> bool:
        """Buffer ``message`` (keyed by its ``message_id``). Returns True if an old
        entry had to be dropped to make room."""
        if not message.message_id:
            raise ValueError("a buffered report needs a message_id")
        if message.WhichOneof("payload") not in REPORT_PAYLOADS:
            raise ValueError("only reports are buffered (never heartbeats or pongs)")
        dropped = False
        if message.message_id in self._entries:
            # Re-appending the same report is a no-op (idempotent caller retry).
            return False
        if len(self._entries) >= self._capacity:
            self._entries.popitem(last=False)
            self.dropped += 1
            dropped = True
        self._entries[message.message_id] = ReplayEntry(sequence, message)
        return dropped

    def ack(self, message_id: str) -> bool:
        """Remove the entry acked by the controller; False if unknown (duplicate
        ack, or the entry was already dropped)."""
        return self._entries.pop(message_id, None) is not None

    def get(self, message_id: str) -> ReplayEntry | None:
        """The buffered entry for ``message_id`` (None once acked or evicted)."""
        return self._entries.get(message_id)

    def pending(self) -> list[ReplayEntry]:
        """Entries in send order (oldest first)."""
        return list(self._entries.values())

    def first_sequence(self) -> int | None:
        """Sequence of the oldest buffered report, or None when empty."""
        for entry in self._entries.values():
            return entry.sequence
        return None

    def clear(self) -> None:
        self._entries.clear()


class RecentIds:
    """Fixed-size set of controller ``message_id`` values for duplicate detection.

    Mirrors the controller's dedupe window: the same command delivered twice (a
    replay across a reconnect) must have exactly one effect but may be acked twice.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._order: deque[str] = deque(maxlen=capacity)
        self._seen: set[str] = set()

    def remember(self, message_id: str) -> bool:
        """Record ``message_id``; True if it was already known."""
        if message_id in self._seen:
            return True
        if len(self._order) == self._order.maxlen:
            evicted = self._order[0]
            self._seen.discard(evicted)
        self._order.append(message_id)
        self._seen.add(message_id)
        return False

    def __len__(self) -> int:
        return len(self._seen)
