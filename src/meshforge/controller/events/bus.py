"""The controller's in-memory event bus (V0.8 G1, D172, ADR-0017).

One process-wide stream of typed events the dashboard follows over
``/v1/events/ws`` instead of polling every resource:

* ``node.status`` ``{node_id, state, connected}`` - a node's liveness or
  session changed;
* ``node.metrics`` ``{node_id, cpu_percent, ram_used_bytes, ram_total_bytes,
  gpus, observed_at, recv_at}`` - at most once a second per node
  (``observed_at`` is the agent's clock, ``recv_at`` the controller's at
  arrival: time latency from ``recv_at``, V0.9 G0);
* ``job.state`` ``{job_id, state}`` and ``attempt.state`` ``{attempt_id,
  job_id, node_id, state}`` - after the database transaction that made the
  change committed (:mod:`.changes`);
* ``alert`` ``{kind, ...}`` - a security refusal an operator should see
  (D180; the ``audit_event`` row stays the source of truth);
* ``campaign.progress`` ``{campaign_id, state, counts, done_percent, eta_s,
  ...}`` - a runner-owned benchmark campaign moved (V0.9 G5; admins only);
* ``split.progress`` ``{job_id, job_state, chunks, items_done, lanes, eta_s,
  ...}`` - a split job's progress, read from its rows (V0.10 G7,
  :mod:`meshforge.controller.split.progress`; the job's owner and admins).

**Order and resume.** Every event gets the next ``seq`` (1, 2, ...) of this
bus; the bus also has an ``epoch`` (random per controller process). The last
:data:`DEFAULT_RING_SIZE` events stay in a ring. A client that reconnects
sends ``since=<last seq seen>&epoch=<epoch>``: if the ring still holds every
event after ``since`` and the epoch is this bus's, it gets exactly those (no
gap, no duplicate); otherwise - the ring moved on, the controller restarted,
a ``since`` from the future - it is told ``resync`` and refetches. Honest
"you missed something" beats a silent gap.

**Bounded.** Each subscriber has a queue of at most ``queue_size`` events. The
publisher never waits: a subscriber whose queue is full is marked
``overflowed`` and dropped (its socket closes ``4008 slow_consumer``; the
client reconnects and resumes or resyncs). Memory is the ring plus the
queues, both bounded, whatever the clients do.

**Who sees what.** Every event carries an ``audience``: ``all`` users (node
events), the ``owner`` of a job (job and attempt events - and every admin), or
``admin`` only (alerts). The WebSocket filters before it sends, the same rule
as the REST routes (D141: another user's job does not exist for you).

Single event loop; ``publish`` is synchronous and never blocks, so it may be
called from a SQLAlchemy commit hook or a session callback.
"""

from __future__ import annotations

import asyncio
import collections
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from meshforge.common.logging import get_logger

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "DEFAULT_RING_SIZE",
    "Audience",
    "BusEvent",
    "BusStats",
    "EventBus",
    "EventType",
    "Resync",
    "Subscriber",
]

log = get_logger(__name__)

DEFAULT_RING_SIZE: Final = 10_000
DEFAULT_QUEUE_SIZE: Final = 1_000
MAX_SUBSCRIBERS: Final = 256


class EventType(StrEnum):
    NODE_STATUS = "node.status"
    NODE_METRICS = "node.metrics"
    JOB_STATE = "job.state"
    ATTEMPT_STATE = "attempt.state"
    ALERT = "alert"
    CAMPAIGN_PROGRESS = "campaign.progress"
    """V0.9 G5 (D246): a runner-owned campaign's counts / ETA (admins only;
    at most once a second per campaign)."""
    SPLIT_PROGRESS = "split.progress"
    """V0.10 G7 (D270): a split job's chunk / lane progress (its owner and
    admins; at most ``[controller.split] progress_event_hz`` per job)."""


class Audience(StrEnum):
    ALL = "all"
    """Every authenticated user (node events: ``GET /v1/nodes`` is open to all)."""
    OWNER = "owner"
    """The job's owner and every admin."""
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class BusEvent:
    seq: int
    type: EventType
    at: float
    """Wall-clock seconds (``time.time``) - for display only."""
    data: Mapping[str, Any]
    audience: Audience = Audience.ALL
    owner_user_id: str | None = None

    def frame(self) -> dict[str, Any]:
        """What a client receives (never the audience bookkeeping)."""
        return {
            "seq": self.seq,
            "type": self.type.value,
            "at": self.at,
            "data": dict(self.data),
        }

    def visible_to(self, user_id: str | None, *, admin: bool) -> bool:
        if admin:
            return True
        if self.audience is Audience.ALL:
            return True
        if self.audience is Audience.OWNER:
            return self.owner_user_id is not None and self.owner_user_id == user_id
        return False


@dataclass(frozen=True, slots=True)
class Resync:
    """The requested resume point is gone: refetch, then follow from ``seq``."""

    reason: str
    """``ring`` (too old), ``epoch`` (another controller run), ``future``."""


@dataclass(slots=True)
class BusStats:
    published: int = 0
    dropped_subscribers: int = 0
    resyncs: int = 0
    refused_subscribers: int = 0


@dataclass(eq=False, slots=True)
class Subscriber:
    """One follower's bounded queue (identity equality)."""

    queue: asyncio.Queue[BusEvent]
    overflowed: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when overflowed (the sender then closes the socket at once)."""

    def push(self, event: BusEvent) -> bool:
        if self.overflowed:
            return False
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True
            self.wake.set()
            return False
        return True


class EventBus:
    """See module docstring."""

    def __init__(
        self,
        *,
        ring_size: int = DEFAULT_RING_SIZE,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        max_subscribers: int = MAX_SUBSCRIBERS,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if ring_size < 1 or queue_size < 1:
            raise ValueError("ring_size and queue_size must be positive")
        self.epoch: Final = secrets.token_hex(8)
        self._ring: collections.deque[BusEvent] = collections.deque(maxlen=ring_size)
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers
        self._subscribers: set[Subscriber] = set()
        self._seq = 0
        self._wall_clock = wall_clock
        self.stats = BusStats()

    @property
    def seq(self) -> int:
        """The newest event's ``seq`` (0 = none yet)."""
        return self._seq

    def __len__(self) -> int:
        return len(self._subscribers)

    def publish(
        self,
        type_: EventType,
        data: Mapping[str, Any],
        *,
        audience: Audience = Audience.ALL,
        owner_user_id: str | None = None,
    ) -> BusEvent:
        """Append one event and fan it out (never blocks, never raises for a
        slow follower)."""
        self._seq += 1
        event = BusEvent(
            seq=self._seq,
            type=type_,
            at=round(self._wall_clock(), 3),
            data=dict(data),
            audience=audience,
            owner_user_id=owner_user_id,
        )
        self._ring.append(event)
        self.stats.published += 1
        for sub in list(self._subscribers):
            if not sub.push(event):
                self._drop(sub)
        return event

    def _drop(self, sub: Subscriber) -> None:
        if sub in self._subscribers:
            self._subscribers.discard(sub)
            self.stats.dropped_subscribers += 1
            log.info("controller.events.slow_subscriber", queue=self._queue_size)

    def subscribe(
        self, *, since: int | None = None, epoch: str | None = None
    ) -> tuple[Subscriber, Resync | None]:
        """A new follower. Events after ``since`` that the ring still holds are
        queued first (as many as fit; a backlog bigger than the queue is a
        ``resync``); ``Resync`` says the client must refetch. Raises
        :class:`OverflowError` past ``max_subscribers``."""
        if len(self._subscribers) >= self._max_subscribers:
            self.stats.refused_subscribers += 1
            raise OverflowError("too many event subscribers")
        sub = Subscriber(queue=asyncio.Queue(self._queue_size))
        resync = self._backlog(sub, since, epoch)
        if resync is not None:
            self.stats.resyncs += 1
        self._subscribers.add(sub)
        return sub, resync

    def _backlog(
        self, sub: Subscriber, since: int | None, epoch: str | None
    ) -> Resync | None:
        if since is None:
            return None
        if epoch is not None and epoch != self.epoch:
            return Resync("epoch")
        if since > self._seq:
            return Resync("future")
        if since == self._seq:
            return None
        oldest = self._ring[0].seq if self._ring else self._seq + 1
        if since < oldest - 1:
            return Resync("ring")
        missed = [e for e in self._ring if e.seq > since]
        if len(missed) > self._queue_size:
            return Resync("ring")
        for event in missed:
            sub.queue.put_nowait(event)
        return None

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)
