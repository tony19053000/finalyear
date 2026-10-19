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

from collections.abc import Awaitable, Callable
from typing import Final

from meshforge.agent.owner_activity import ActivityState, OwnerActivity
from meshforge.agent.policy import LivePolicy
from meshforge.common.logging import get_logger

__all__ = ["OWNER_ACTIVE", "PAUSED", "OwnerGuard"]

log = get_logger(__name__)

PAUSED: Final = "paused"
"""The owner paused benchmarks (policy ``pause_benchmarks``)."""
OWNER_ACTIVE: Final = "owner_active"


class OwnerGuard:
    """See module docstring. One event loop."""

    def __init__(
        self,
        *,
        policy: LivePolicy | None,
        activity: OwnerActivity | None,
        preempt: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self._policy = policy
        self._activity = activity
        self.preempt = preempt
        self._was: str | None = None
        self.preempted = 0

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
        """Re-evaluate; on the guard turning on, pre-empt the running benchmark
        attempts. The attempts stopped."""
        now = self.blocked()
        before = self._was
        if now is None or before is not None or self.preempt is None:
            self._was = now
            if now != before:
                log.info("agent.bench.guard", blocked=now)
            return 0
        # fix r1 T1-1: remembered as "on" only once the pre-emption succeeded -
        # one that raised is retried at the next evaluation
        stopped = await self.preempt(now)
        self._was = now
        self.preempted += stopped
        log.info("agent.bench.guard", blocked=now, preempted=stopped)
        return stopped

    async def on_activity(self, _state: ActivityState) -> None:
        await self.refresh()
