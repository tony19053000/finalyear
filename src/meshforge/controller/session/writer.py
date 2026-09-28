"""Per-session ordered single-writer for registry writes (V0.3 G2 review, B1).

Liveness is decided in memory; the database is a *record* of it. Neither the
stream's reader task nor the single lease timer may ever await a database
round-trip: with PostgreSQL slow or down (connect 10 s, command 30 s, lease 6 s)
an awaited write would let every healthy node walk ONLINE -> SUSPECT -> OFFLINE
and stall the evaluation of every other node's lease.

So each :class:`~meshforge.controller.session.manager.LiveSession` owns one
:class:`WriteChain`: a bounded FIFO of *write factories* (zero-argument callables
returning the registry coroutine) drained by exactly one asyncio task. Callers
enqueue and return immediately; the chain preserves order (``set_state(SUSPECT)``
-> ``set_state(ONLINE)`` -> ``close_session`` reach the database in that order
even when the first is slow), logs a failed write as
``registry.write_failed{node_id, session_id, op, error}`` and moves on, and ends
by itself after the session's final write (the close reason).

**Lingering sessions (V0.3 G6, D19).** A session that ended *abruptly* keeps its
lease running on a lingering entry, and that entry may still need to write
``OFFLINE`` when the lease expires - on this same chain, so it can never be
reordered against the session's earlier writes. Its ``close_session`` is therefore
enqueued as :attr:`WriteKind.CLOSE` - persisted promptly, in order, never dropped,
but *not* the end of the chain - and the chain is ended later by :meth:`finish`
(no database write) once the linger resolves (lease expiry, a reconnect, or
controller shutdown).

Overflow policy (``capacity`` pending writes, default 1000): a new write that
finds the queue full first evicts the **oldest droppable** write (``touch`` /
``rtt`` - a later one supersedes it anyway). If nothing is droppable, a droppable
newcomer is dropped and a state/end write is still queued (never lost) with an
error log - state writes are paced by the lease timer, so that branch means the
database has been unreachable for on the order of a thousand lease intervals.

Overflow is visible (V0.4 D43): the first drop of an **overflow episode** logs
``registry.write_queue_overflow`` (warning) with ``dropped`` / ``capacity`` /
``pending``; later drops of the same episode are only counted (and logged
``registry.write_dropped`` at debug). The episode ends once the queue has drained
below half its capacity, so a database that recovers and stalls again is reported
again. :attr:`WriteChain.overflow_episodes` counts episodes for the session's
summary.

Time: none. Nothing here reads a clock; ordering is the queue's.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from meshforge.common.errors import MeshForgeError
from meshforge.common.logging import get_logger

__all__ = ["DEFAULT_WRITE_CAPACITY", "WriteChain", "WriteKind"]

log = get_logger(__name__)

DEFAULT_WRITE_CAPACITY: Final = 1000

type WriteFactory = Callable[[], Coroutine[Any, Any, Any]]


class WriteKind(StrEnum):
    """What a queued write is, which decides whether overflow may drop it."""

    TOUCH = "touch"
    """``last_seen_at`` refresh - droppable (the next one supersedes it)."""
    RTT = "rtt"
    """RTT sample - droppable (the next sample supersedes it)."""
    STATE = "state"
    """Liveness transition - never dropped (order and presence matter)."""
    SCORES = "scores"
    """Hardware-score run (roadmap 0.3.4, D38/D39) - never dropped **from the
    queue** on overflow. That is not "never lost": the agent was acked when the
    report arrived, so a write the DB refuses is logged (``registry.write_failed``)
    and lost until the next Hello re-delivers the cached run. Rare (about one per
    node lifetime), so it cannot fill the queue."""
    END = "end"
    """``close_session`` - never dropped; the chain ends after it."""
    CLOSE = "close"
    """``close_session`` of a session whose lease lingers (D19) - never dropped,
    and the chain stays open for the linger's own final state write; it is ended
    by :meth:`WriteChain.finish`."""

    @property
    def droppable(self) -> bool:
        return self in (WriteKind.TOUCH, WriteKind.RTT)


@dataclass(frozen=True, slots=True)
class _Pending:
    op: str
    kind: WriteKind
    factory: WriteFactory


class WriteChain:
    """See module docstring. One per live session; created by the manager."""

    def __init__(
        self,
        *,
        node_id: str,
        session_id: str,
        capacity: int = DEFAULT_WRITE_CAPACITY,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._node_id = node_id
        self._session_id = session_id
        self._capacity = capacity
        self._queue: deque[_Pending] = deque()
        self._wake = asyncio.Event()
        self._closed = False
        """True once the END write was enqueued or :meth:`finish` was called;
        later writes are refused."""
        self._task: asyncio.Task[None] | None = None
        # counters (read by tests and, later, telemetry)
        self.enqueued = 0
        self.completed = 0
        self.failed = 0
        self.dropped = 0
        self.overflow_episodes = 0
        """Overflow episodes so far (D43); one warning each."""
        self._overflowing = False
        """True from an episode's first drop until the queue is below half."""

    # ----------------------------------------------------------------- state

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    def done(self) -> bool:
        return self._task is not None and self._task.done()

    # --------------------------------------------------------------- enqueue

    def start(self) -> asyncio.Task[None]:
        """Spawn the single consumer task (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name=f"registry-chain:{self._session_id}"
            )
        return self._task

    def enqueue(self, op: str, kind: WriteKind, factory: WriteFactory) -> bool:
        """Queue one write; return False if it was refused or dropped.

        Never awaits and never raises for a full queue (see the overflow policy).
        """
        if self._closed:
            self.dropped += 1
            log.error(
                "registry.write_after_close",
                node_id=self._node_id,
                session_id=self._session_id,
                op=op,
                kind=kind.value,
            )
            return False
        if len(self._queue) >= self._capacity and not self._make_room(op, kind):
            return False
        self._queue.append(_Pending(op, kind, factory))
        self.enqueued += 1
        if kind is WriteKind.END:
            self._closed = True
        self._wake.set()
        return True

    def _make_room(self, op: str, kind: WriteKind) -> bool:
        """Queue is full: evict the oldest droppable write, else decide the newcomer.

        Returns True when the newcomer may be appended.
        """
        for index, pending in enumerate(self._queue):
            if pending.kind.droppable:
                del self._queue[index]
                self._record_drop(pending.op, pending.kind, "overflow_evicted_oldest")
                return True
        if kind.droppable:
            self._record_drop(op, kind, "overflow_newest_droppable")
            return False
        # A state/end write with nothing droppable ahead of it: keep it (it is
        # paced by the lease timer, so this is not a peer-controlled growth path)
        # and shout - the database has been unreachable for a very long time.
        log.error(
            "registry.write_queue_over_capacity",
            node_id=self._node_id,
            session_id=self._session_id,
            op=op,
            kind=kind.value,
            pending=len(self._queue),
            capacity=self._capacity,
        )
        return True

    def _record_drop(self, op: str, kind: WriteKind, reason: str) -> None:
        """Count one dropped write; warn once per overflow episode (D43)."""
        self.dropped += 1
        if not self._overflowing:
            self._overflowing = True
            self.overflow_episodes += 1
            log.warning(
                "registry.write_queue_overflow",
                node_id=self._node_id,
                session_id=self._session_id,
                dropped=self.dropped,
                capacity=self._capacity,
                pending=len(self._queue),
                episode=self.overflow_episodes,
            )
        log.debug(
            "registry.write_dropped",
            node_id=self._node_id,
            session_id=self._session_id,
            op=op,
            kind=kind.value,
            pending=len(self._queue),
            reason=reason,
        )

    def _note_drained(self) -> None:
        """An overflow episode ends once the queue is below half capacity."""
        if self._overflowing and len(self._queue) < self._capacity / 2:
            self._overflowing = False

    def finish(self) -> None:
        """No more writes: the task ends once everything already queued is done.

        Idempotent. The end of a chain whose ``close_session`` went out as
        :attr:`WriteKind.CLOSE` (a lingering session, D19); also used by
        :meth:`drain`.
        """
        if not self._closed:
            self._closed = True
            self._wake.set()

    # ---------------------------------------------------------------- consume

    async def _run(self) -> None:
        while True:
            while not self._queue:
                if self._closed:
                    return  # END was consumed (or nothing more can arrive)
                await self._wake.wait()
                self._wake.clear()
            pending = self._queue.popleft()
            self._note_drained()
            await self._execute(pending)
            if pending.kind is WriteKind.END:
                return

    async def _execute(self, pending: _Pending) -> None:
        try:
            await pending.factory()
        except asyncio.CancelledError:
            raise
        except MeshForgeError as exc:
            self.failed += 1
            log.exception(
                "registry.write_failed",
                node_id=self._node_id,
                session_id=self._session_id,
                op=pending.op,
                kind=pending.kind.value,
                error=str(exc),
                failure_type=exc.failure_type.value,
                retryable=exc.retryable,
            )
        except Exception as exc:
            # Not a typed registry failure: still a bug in one write, never a
            # reason to stop persisting the rest of this session's history.
            self.failed += 1
            log.exception(
                "registry.write_failed",
                node_id=self._node_id,
                session_id=self._session_id,
                op=pending.op,
                kind=pending.kind.value,
                error=str(exc),
                failure_type="internal_error",
            )
        else:
            self.completed += 1

    # ------------------------------------------------------------------ drain

    async def drain(self, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for the chain to finish; cancel it otherwise.

        Returns True if every queued write reached the registry (or failed and was
        logged) before the deadline. Used by ``SessionManager.shutdown``.
        """
        task = self._task
        if task is None:
            return True
        # No END will ever arrive: let the consumer stop once the queue is empty.
        self.finish()
        _done, still = await asyncio.wait({task}, timeout=timeout_s)
        if not still:
            return True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        log.error(
            "registry.write_abandoned",
            node_id=self._node_id,
            session_id=self._session_id,
            pending=len(self._queue) + 1,  # + the write that was in flight
            timeout_s=timeout_s,
        )
        return False
