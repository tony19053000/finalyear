"""The agent's owner guard for benchmark jobs (V0.9 G5, roadmap 0.9.7, D219/D246).

Two inputs, one answer - "may a benchmark job run here now?":

* the owner's **manual switch**: policy ``pause_benchmarks``
  (``meshforge-agent bench pause|resume``, :mod:`meshforge.agent.policy`);
* **owner activity**: :class:`~meshforge.agent.owner_activity.OwnerActivity`.

:meth:`OwnerGuard.blocked` is what the executor asks at every benchmark
``StartAttempt`` (refused ``owner_preempted``). :meth:`OwnerGuard.refresh`
runs after every change of either input; when the guard turns on it
pre-empts the running benchmark attempts
(:meth:`~meshforge.agent.execution.executor.Executor.preempt_benchmarks`:
conditions reverted first, then the attempts cancelled). The heartbeat carries
the activity bit (:meth:`OwnerGuard.heartbeat_fields`); the pause switch
reaches the controller in the policy (``PolicyUpdate``). User jobs are never
touched by any of it.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Final

from meshforge.agent.owner_activity import ActivityState, OwnerActivity
from meshforge.agent.policy import LivePolicy
from meshforge.common.logging import get_logger

__all__ = ["MAX_RETRY_S", "OWNER_ACTIVE", "PAUSED", "OwnerGuard"]

log = get_logger(__name__)

PAUSED: Final = "paused"
"""The owner paused benchmarks (policy ``pause_benchmarks``)."""
OWNER_ACTIVE: Final = "owner_active"
MAX_RETRY_S: Final = 30.0
"""Longest wait between two tries of a pre-emption that keeps failing."""


class OwnerGuard:
    """See module docstring. One event loop.

    **Retry (fix r2, D251 B1/B2).** A pre-emption that raised is retried while
    the guard stays on: at the next activity poll, then after 2, 4, 8 ... poll
    intervals (by the clock), at most :data:`MAX_RETRY_S` apart - one warning per
    failed step, never a storm. A pre-emption that succeeded is not repeated
    until the guard turned off and on again."""

    def __init__(
        self,
        *,
        policy: LivePolicy | None,
        activity: OwnerActivity | None,
        preempt: Callable[[str], Awaitable[int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float | None = None,
    ) -> None:
        self._policy = policy
        self._activity = activity
        self.preempt = preempt
        self._clock = clock
        self._poll_s = (
            poll_s
            if poll_s is not None
            else (activity.poll_s if activity is not None else 2.0)
        )
        self._was: str | None = None
        """The guard reason a pre-emption succeeded for (None = off, or pending)."""
        self._pending: str | None = None
        self._failures = 0
        self._retry_at: float | None = None
        self.preempted = 0
        if activity is not None:
            activity.on_poll = self.on_poll  # retries ride on every poll

    def blocked(self) -> str | None:
        """Why benchmark jobs may not run here now (None = they may)."""
        if self._policy is not None and self._policy.policy.pause_benchmarks:
            return PAUSED
        if self._activity is not None and self._activity.state.active:
            reason = self._activity.state.reason
            return f"{OWNER_ACTIVE}:{reason}" if reason else OWNER_ACTIVE
        return None

    def heartbeat_fields(self) -> tuple[bool, str, str]:
        """``(owner_active, reason, idle_source)`` for the heartbeat."""
        if self._activity is None:
            return False, "", ""
        state = self._activity.state
        return state.active, state.reason if state.active else "", state.idle_source

    async def refresh(self) -> int:
        """Re-evaluate (an input changed); on the guard turning on, pre-empt the
        running benchmark attempts. Raises what the pre-emption raised (it is
        retried by :meth:`on_poll`). The attempts stopped."""
        now = self.blocked()
        if now is None or self.preempt is None:
            if now != self._was or self._pending is not None:
                log.info("agent.bench.guard", blocked=now)
            self._was, self._pending = now, None
            self._failures, self._retry_at = 0, None
            return 0
        if self._was is not None:
            self._was = now  # still on (maybe for another reason): done already
            return 0
        return await self._preempt(now)

    async def _preempt(self, now: str) -> int:
        assert self.preempt is not None  # noqa: S101 - callers checked
        self._pending = now
        try:
            stopped = await self.preempt(now)
        except Exception:
            self._failures += 1
            # one poll first, then 2, 4, 8 ... poll intervals, at most MAX_RETRY_S
            delay = min(MAX_RETRY_S, self._poll_s * 2 ** (self._failures - 1))
            self._retry_at = None if self._failures == 1 else self._clock() + delay
            log.warning(
                "agent.bench.preempt_failed",
                blocked=now,
                failures=self._failures,
                retry_in_s=0.0 if self._retry_at is None else round(delay, 1),
            )
            raise
        # fix r1 T1-1: remembered as "on" only once the pre-emption succeeded
        self._was, self._pending = now, None
        self._failures, self._retry_at = 0, None
        self.preempted += stopped
        log.info("agent.bench.guard", blocked=now, preempted=stopped)
        return stopped

    async def on_poll(self, _state: ActivityState | None = None) -> None:
        """Every activity poll: retry a pending pre-emption when it is due
        (never raises - the failure is logged by :meth:`_preempt`)."""
        if self._pending is None or self.preempt is None:
            return
        now = self.blocked()
        if now is None:
            await self.refresh()
            return
        if self._retry_at is not None and self._clock() < self._retry_at:
            return
        with contextlib.suppress(Exception):
            await self._preempt(now)

    async def on_activity(self, _state: ActivityState) -> None:
        await self.refresh()
