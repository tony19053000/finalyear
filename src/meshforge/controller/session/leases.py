"""The single lease timer for all sessions (roadmap 0.3.7 wiring, 0.3.10 pings).

One task, not one per session: it sleeps until the earliest
:meth:`LeaseTracker.deadline` (or the next ping due) across every live session
(at most ``max_idle_s`` when nothing is scheduled), and is woken early by
``SessionManager.wake`` when a session registers. Deadlines only ever move later
(heartbeats extend leases, pings reschedule themselves), so sleeping until the
known minimum is safe; only a *new* session can create an earlier one, and that
sets ``wake``. On each tick it

1. drains the due lease events of every session (``LEASE_MISSED`` then
   ``LEASE_EXPIRED`` - a stalled loop still walks ONLINE -> SUSPECT -> OFFLINE);
2. applies them through :meth:`SessionManager.apply_event` (which queues the
   registry write on the session's chain and logs ``node.state_changed``; an
   ``InvalidTransitionError`` there is logged, never raised);
3. closes a session whose lease expired (``end_reason = lease_expired``);
4. sends one RTT ``Ping`` per lease interval to every ONLINE session;
5. flushes a coalesced ``last_seen_at`` write for sessions whose heartbeats since
   the last write were only recorded in memory (``SessionManager.flush_touch``);
6. walks the **lingering** entries too - sessions that ended abruptly and are
   SUSPECT with their lease still running (D19, ARCHITECTURE §12.3 2026-09-24):
   at the normal lease edge (from the last heartbeat) ``LEASE_EXPIRED`` writes
   OFFLINE on the entry's own chain and ends the linger
   (``SessionManager.expire_linger``). A lingering entry has no stream, so it gets
   no ping and no touch. A new lingering entry sets ``wake`` like a new session;
7. skips (and takes no deadline from) any session or lingering entry a newer
   ``Hello`` is being admitted for (``SessionManager.admission_pending``, D28):
   that Hello owns the node's state, so no lease event is decided against it
   meanwhile. The admission wakes the timer when it returns.

The timer never awaits the database: every write it causes is queued on the
session's :class:`~.writer.WriteChain`, so one node's slow row can never delay
another node's lease evaluation (review B1).

Clock: the injected monotonic ``clock`` only (``loop.time()`` in production).
:meth:`LeaseTimer.tick` is the pure-ish step unit tests drive with a fake clock;
:meth:`LeaseTimer.run` is the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from meshforge.common.logging import get_logger
from meshforge.controller.liveness import LivenessEvent, NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.manager import LiveSession, SessionManager

__all__ = ["LeaseTimer"]

log = get_logger(__name__)


class LeaseTimer:
    """See module docstring."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        clock: Callable[[], float],
        max_idle_s: float = 1.0,
    ) -> None:
        if not max_idle_s > 0:
            raise ValueError("max_idle_s must be > 0")
        self._manager = manager
        self._clock = clock
        self._max_idle_s = max_idle_s
        self.ticks = 0

    async def tick(self, now: float) -> float | None:
        """Process everything due at ``now``; return the next monotonic deadline.

        ``None`` means nothing is scheduled (no sessions, or every tracker has fired
        both events and no ping is pending).
        """
        self.ticks += 1
        next_deadline: float | None = None
        interval = self._manager.liveness.heartbeat_interval_s
        for live in self._manager.snapshot():
            if self._manager.admission_pending(live):
                continue  # D28: the pending Hello owns this node's state
            await self._tick_session(live, now)
            if live.ended:
                continue
            await self._manager.flush_touch(live, now)
            for candidate in (
                live.tracker.deadline(),
                live.next_ping_at if live.state is NodeState.ONLINE else None,
                live.touch_due_at(interval),
            ):
                if candidate is not None and (
                    next_deadline is None or candidate < next_deadline
                ):
                    next_deadline = candidate
        for entry in self._manager.lingering():
            if self._manager.admission_pending(entry):
                continue  # D28: resolved by that admission, or re-woken after it
            self._manager.expire_linger(entry, now)
            candidate = entry.tracker.deadline() if entry.lingering else None
            if candidate is not None and (
                next_deadline is None or candidate < next_deadline
            ):
                next_deadline = candidate
        return next_deadline

    async def _tick_session(self, live: LiveSession, now: float) -> None:
        while not live.ended:
            event = live.tracker.check(now)
            if event is None:
                break
            silence = live.tracker.silence(now)
            await self._manager.apply_event(live, event, silence_s=silence)
            if event is LivenessEvent.LEASE_EXPIRED:
                log.warning(
                    "node.lease_expired",
                    node_id=live.node_id,
                    session_id=live.session_id,
                    silence_s=round(silence, 3),
                )
                await self._manager.end_session(live, SessionEndReason.LEASE_EXPIRED)
                return
        if (
            not live.ended
            and live.state is NodeState.ONLINE
            and now >= live.next_ping_at
        ):
            await self._manager.send_ping(live, now)

    def timeout_until(self, next_deadline: float | None, now: float) -> float:
        """How long :meth:`run` sleeps before the next tick (0 = already due)."""
        if next_deadline is None:
            return self._max_idle_s
        return max(next_deadline - now, 0.0)

    async def run(self) -> None:
        """Loop forever (cancelled by the server on shutdown)."""
        wake = self._manager.wake
        while True:
            now = self._clock()
            try:
                next_deadline = await self.tick(now)
            except asyncio.CancelledError:
                raise
            except Exception:  # the timer must outlive any bug in a tick
                log.exception("lease_timer.tick_failed")
                next_deadline = None
            timeout = self.timeout_until(next_deadline, self._clock())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout)
            wake.clear()
