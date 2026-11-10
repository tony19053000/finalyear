"""Event-loop stall watch for the agent (V1.0 G10, D444).

Every heartbeat, Pong, Ack and report the agent sends is written by one asyncio
event loop. When that loop does not run - the whole process paged out on a
laptop with almost no free RAM, a long GC pause, a blocking call on the loop -
the controller hears nothing, and after ``offline_after_s`` (6 s, three
heartbeats) it ends the session ``lease_expired``. On the agent side that looks
exactly like a controller fault: the stream ends with ``ABORTED lease_expired``
while the agent "was connected the whole time" (lab L3, 2026-11-10, 0.7 GiB free
of 7.7 GiB, commit 87 %).

This module makes the cause visible instead of guessed. One small task sleeps
``interval_s`` and measures how late it wakes up. A wake-up later than
``threshold_s`` is a **stall** of this process's loop:

* it is logged ``agent.loop.stalled`` (warning) with the lag and, when psutil is
  available, the machine's available memory - so a lab log shows "the agent did
  not run for 7.4 s at 210 MB available" next to the ``lease_expired``;
  at most one line per ``log_every_s`` (the stalls in between are counted in
  the next line's ``suppressed``/``worst_s``);
* :meth:`LoopWatch.max_lag_s` answers "the worst stall of the last N seconds";
  the session client logs it with every ``agent.session.stream_ended``
  (``loop_lag_max_s``), so a lease loss caused by this laptop is told apart from
  a network or controller fault in one line.

It does not *prevent* a stall: nothing inside a process that is not scheduled can
send a heartbeat (a heartbeat thread would stall with it - paging stops every
thread, and gRPC aio writes must be made from the loop). The remedy is on the
laptop (free memory) and, for long work, on the controller (D444 follow-up).

Time: the injected monotonic ``clock`` only (``loop.time()`` in production);
:meth:`LoopWatch.observe` is the pure step unit tests drive.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Final

from meshforge.common.logging import get_logger

__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_THRESHOLD_S",
    "LoopWatch",
    "available_memory_mb",
]

log = get_logger(__name__)

DEFAULT_INTERVAL_S: Final = 0.5
"""How often the watch wakes up (its cost: one timer per half second)."""
DEFAULT_THRESHOLD_S: Final = 1.0
"""A wake-up this late is a stall: half the default 2 s heartbeat interval -
two such stalls back to back already cost the controller a heartbeat."""
DEFAULT_LOG_EVERY_S: Final = 10.0
"""At most one ``agent.loop.stalled`` line per this many seconds."""
_KEEP: Final = 256
"""Stalls remembered for :meth:`LoopWatch.max_lag_s` (bounded memory)."""


def available_memory_mb() -> int | None:
    """The machine's available memory in MiB, or None (psutil missing/failed)."""
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - psutil is an agent dependency
        return None
    try:
        return int(psutil.virtual_memory().available // (1024 * 1024))
    except (OSError, RuntimeError, AttributeError):  # pragma: no cover - platform
        return None


class LoopWatch:
    """See module docstring."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        node_id: str | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        threshold_s: float = DEFAULT_THRESHOLD_S,
        log_every_s: float = DEFAULT_LOG_EVERY_S,
        memory_mb: Callable[[], int | None] = available_memory_mb,
    ) -> None:
        if not interval_s > 0:
            raise ValueError("interval_s must be > 0")
        if not threshold_s > 0:
            raise ValueError("threshold_s must be > 0")
        if log_every_s < 0:
            raise ValueError("log_every_s must be >= 0")
        self._clock = clock
        self._node_id = node_id
        self.interval_s = interval_s
        self.threshold_s = threshold_s
        self._log_every_s = log_every_s
        self._memory_mb = memory_mb
        self._stalls: deque[tuple[float, float]] = deque(maxlen=_KEEP)
        self._logged_at: float | None = None
        self._suppressed = 0
        self._worst_suppressed = 0.0
        self.stalls = 0
        """Stalls seen since start (tests, status)."""
        self.worst_s = 0.0
        """The worst lag seen since start."""
        self._expected: float | None = None
        """When the running watch's pending wake-up is due (None: not running)."""

    def observe(self, expected: float, now: float) -> float | None:
        """One wake-up meant for ``expected`` happened at ``now``.

        Returns the lag when it is a stall (and records/logs it), else None.
        """
        lag = now - expected
        if lag <= self.threshold_s:
            return None
        self.stalls += 1
        self.worst_s = max(self.worst_s, lag)
        self._stalls.append((now, lag))
        if self._logged_at is not None and now - self._logged_at < self._log_every_s:
            self._suppressed += 1
            self._worst_suppressed = max(self._worst_suppressed, lag)
            return lag
        log.warning(
            "agent.loop.stalled",
            node_id=self._node_id,
            lag_s=round(lag, 3),
            available_mb=self._memory_mb(),
            suppressed=self._suppressed,
            worst_suppressed_s=round(self._worst_suppressed, 3)
            if self._suppressed
            else None,
            hint="this agent process did not run; heartbeats were not sent meanwhile "
            "(low free memory / paging, a blocked event loop)",
        )
        self._logged_at = now
        self._suppressed = 0
        self._worst_suppressed = 0.0
        return lag

    def max_lag_s(self, window_s: float, now: float | None = None) -> float:
        """The worst stall that ended within the last ``window_s`` (0.0: none).

        Also the **in-flight** stall (D444 r1): right after the loop resumes, a
        callback queued during the stall (a stream end) may run before the
        watch's own late wake-up has recorded it; the pending wake-up's lateness
        counts then too, once it is past ``threshold_s``.
        """
        at = self._clock() if now is None else now
        worst = 0.0
        if self._expected is not None:
            pending = at - self._expected
            if pending > self.threshold_s:
                worst = pending
        for when, lag in reversed(self._stalls):  # newest first
            if at - when > window_s:
                break  # this one and every older one ended before the window
            worst = max(worst, lag)
        return worst

    async def run(self) -> None:
        """Until cancelled."""
        while True:
            expected = self._clock() + self.interval_s
            self._expected = expected
            try:
                await asyncio.sleep(self.interval_s)
            finally:
                self._expected = None
            self.observe(expected, self._clock())
