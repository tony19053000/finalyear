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
   the last write were only recorded in memory (``SessionManager.flush_touch``),
   and re-sends commands whose Ack is overdue (V0.6 D113,
   ``CommandChannel.resend_due``);
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

**A stall of the controller is not silence of the agent (V1.0 G7, D424).** The
timer sleeps until a known deadline; if it wakes up more than
``stall_tolerance_s`` *after* that deadline, the controller's own event loop was
not running (a GC pause, a VM steal, a CPU-starved host, a blocking call) and the
heartbeats that arrived meanwhile may still sit unread in the sockets. Judging a
lease at that instant expires live nodes (CI run 40733787742: a 1.3 s stall,
``lease_expired``, the agent in BACKOFF). So when such a late wake-up finds a lease
event due, the timer first *listens* for one heartbeat interval and judges the
leases afterwards; a node that is really silent is still expired, one interval
later. At most ``max_stall_deferrals`` deferrals in a row, so a controller that
keeps stalling still expires dead nodes (worst-case extra delay
``max_stall_deferrals x heartbeat_interval_s``). Each deferral logs
``lease_timer.stall_deferred`` and counts in :attr:`LeaseTimer.stall_deferrals`.

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

__all__ = ["STALL_TOLERANCE_SHARE", "LeaseTimer"]

log = get_logger(__name__)

#: D424: a wake-up later than this share of a heartbeat interval is a stall.
STALL_TOLERANCE_SHARE = 0.1


class LeaseTimer:
    """See module docstring."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        clock: Callable[[], float],
        max_idle_s: float = 1.0,
        stall_tolerance_s: float | None = None,
        max_stall_deferrals: int = 2,
    ) -> None:
        if not max_idle_s > 0:
            raise ValueError("max_idle_s must be > 0")
        interval = manager.liveness.heartbeat_interval_s
        if stall_tolerance_s is None:
            # ordinary wake-up jitter is milliseconds; 10 % of an interval is a stall
            stall_tolerance_s = STALL_TOLERANCE_SHARE * interval
        if not stall_tolerance_s >= 0:
            raise ValueError("stall_tolerance_s must be >= 0")
        if max_stall_deferrals < 0:
            raise ValueError("max_stall_deferrals must be >= 0")
        self._manager = manager
        self._clock = clock
        self._max_idle_s = max_idle_s
        self._stall_tolerance_s = stall_tolerance_s
        self._max_stall_deferrals = max_stall_deferrals
        self.ticks = 0
        self.stall_deferrals = 0
        """Lease judgements put off because the timer itself woke up late (D424)."""

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
                self._manager.commands.resend_due(live, now),  # D113
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

    def lease_event_due(self, now: float) -> bool:
        """Would :meth:`tick` at ``now`` decide a lease event for a live session?

        Read-only (``deadline()`` never mutates a tracker). Lingering entries are
        not counted: their stream is gone, so no heartbeat can be waiting for them.
        """
        for live in self._manager.snapshot():
            if live.ended or self._manager.admission_pending(live):
                continue
            deadline = live.tracker.deadline()
            if deadline is not None and deadline <= now:
                return True
        return False

    def stall_deferral(
        self, now: float, expected: float | None, deferred: int
    ) -> float:
        """D424: how long to listen before judging leases (0 = judge now).

        ``expected`` is when the timer meant to wake up (None: no scheduled
        wake-up, e.g. the first tick); ``deferred`` counts the deferrals in a row
        so far.
        """
        if expected is None or deferred >= self._max_stall_deferrals:
            return 0.0
        late = now - expected
        if late <= self._stall_tolerance_s or not self.lease_event_due(now):
            return 0.0
        return self._manager.liveness.heartbeat_interval_s

    async def run(self) -> None:
        """Loop forever (cancelled by the server on shutdown)."""
        wake = self._manager.wake
        expected: float | None = None  # when the last sleep meant to end
        deferred = 0  # D424 deferrals in a row
        while True:
            now = self._clock()
            listen = self.stall_deferral(now, expected, deferred)
            if listen > 0:
                deferred += 1
                self.stall_deferrals += 1
                log.warning(
                    "lease_timer.stall_deferred",
                    late_s=round(now - (expected or now), 3),
                    listen_s=round(listen, 3),
                    deferral=deferred,
                )
                expected = now + listen
                # not woken by ``wake``: a new session cannot need judging sooner
                await asyncio.sleep(listen)
                continue
            deferred = 0
            try:
                next_deadline = await self.tick(now)
            except asyncio.CancelledError:
                raise
            except Exception:  # the timer must outlive any bug in a tick
                log.exception("lease_timer.tick_failed")
                next_deadline = None
            after = self._clock()
            timeout = self.timeout_until(next_deadline, after)
            # Woken early by ``wake`` (a new session) is not lateness: ``now`` is
            # then before ``expected`` and :meth:`stall_deferral` returns 0.
            expected = after + timeout
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout)
            wake.clear()
