"""A chunk that makes no progress is reported (V1.0 G10 BUG-4, D451).

Lab L4 (job_01M73B13999W9AWDT89F7KDX2H chunk 68): the chunk's worker sat for
17 minutes with no output and ~0 CPU. The lane is a separate task, so it kept
renewing the lease every third of the lease timeout; the controller only copied
the chunk when its queue ran empty (V0.10 G6 speculation), and the job's tail
waited on it.

**The rule.** While a chunk's process runs, the lane samples the CPU time of its
whole process tree (:func:`tree_cpu_seconds`, psutil) every ``check_s`` and
notes every output byte. The chunk is *progressing* while it writes output or
uses more than ``cpu_share`` CPU-seconds per wall second; otherwise it is idle.
Idle for at least

    threshold = max(floor_s, factor x expected)

is **stalled**. ``expected`` is this lane's own recent per-item run time
(:class:`PaceHistory`: the median of the last chunks, so one hung chunk does not
raise it) times the chunk's item count; before the lane has finished a chunk,
the floor alone applies.

**Report, never kill.** A chunk that waits on a GPU kernel or on the network
can be quiet for a while and still be fine, so the lane does not stop it: it
logs ``agent.lane.chunk_stalled`` once per stall and sends ``stalled_s`` with
the lease in every ``LeaseRenew`` (``ChunkLeaseRef.stalled_s``). The controller
decides (an early speculative copy elsewhere - first result wins - or a reclaim).
CPU that cannot be read is no evidence: never a stall.

Settings: ``[agent.split] stall_watch / stall_floor_s / stall_factor /
stall_cpu_share / stall_check_s``. Time: the injected monotonic clock.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from meshforge.common.logging import get_logger

__all__ = [
    "ChunkProgress",
    "PaceHistory",
    "StallPolicy",
    "tree_cpu_seconds",
    "watch_chunk",
]

log = get_logger(__name__)

PACE_KEEP: Final = 20
"""Finished chunks a lane remembers for its expected per-item time."""


@dataclass(frozen=True, slots=True)
class StallPolicy:
    enabled: bool
    floor_s: float
    factor: float
    cpu_share: float
    check_s: float

    @classmethod
    def from_settings(cls, split: Any) -> StallPolicy:
        """From ``[agent.split]`` (:class:`~meshforge.agent.settings.SplitSettings`)."""
        return cls(
            enabled=bool(split.stall_watch),
            floor_s=float(split.stall_floor_s),
            factor=float(split.stall_factor),
            cpu_share=float(split.stall_cpu_share),
            check_s=float(split.stall_check_s),
        )

    def threshold_s(self, expected_s: float | None) -> float:
        if expected_s is None:
            return self.floor_s
        return max(self.floor_s, self.factor * expected_s)


class PaceHistory:
    """Per-item run seconds of a lane's last finished chunks."""

    def __init__(self, keep: int = PACE_KEEP) -> None:
        self._per_item: deque[float] = deque(maxlen=max(1, keep))

    def add(self, run_s: float, *, items: int) -> None:
        if items <= 0 or run_s < 0:
            return
        self._per_item.append(run_s / items)

    def expected_s(self, items: int) -> float | None:
        if not self._per_item or items <= 0:
            return None
        return statistics.median(self._per_item) * items


class ChunkProgress:
    """The pure judgement for one running chunk (module docstring)."""

    def __init__(
        self, policy: StallPolicy, *, started: float, expected_s: float | None
    ) -> None:
        self._policy = policy
        self._expected_s = expected_s
        self._idle_since = started
        self._last: tuple[float, float] | None = (started, 0.0)
        self.stalled = False

    def begin(self, now: float) -> None:
        """The chunk's process exists from ``now`` (the idle clock starts)."""
        self._idle_since = now
        self._last = (now, 0.0)

    def output(self, now: float) -> None:
        self._idle_since = now

    def sample(self, now: float, cpu_s: float | None) -> float:
        """Feed one CPU reading; returns ``stalled_s`` (0.0 = not stalled)."""
        if not self._policy.enabled:
            return 0.0
        if cpu_s is None:
            # no evidence is not a stall: the idle clock restarts
            self._idle_since = now
            self._last = None
        elif self._last is None:
            self._last = (now, cpu_s)
        else:
            at, before = self._last
            if now > at:
                if (cpu_s - before) / (now - at) > self._policy.cpu_share:
                    self._idle_since = now
                self._last = (now, cpu_s)
        idle = now - self._idle_since
        stalled = idle if idle >= self._policy.threshold_s(self._expected_s) else 0.0
        self.stalled = stalled > 0
        return stalled


def tree_cpu_seconds(pid: int) -> float | None:
    """User + system CPU seconds of ``pid`` and its descendants (None: gone or
    unreadable)."""
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - psutil is an agent dependency
        return None
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return None
    total = 0.0
    for proc in procs:
        try:
            times = proc.cpu_times()
        except (psutil.Error, OSError):
            if proc is root:
                return None
            continue  # a descendant that just ended
        total += float(times.user) + float(times.system)
    return total


async def watch_chunk(
    progress: ChunkProgress,
    policy: StallPolicy,
    *,
    clock: Callable[[], float],
    pid: Callable[[], int | None],
    report: Callable[[float], None],
    cpu: Callable[[int], float | None] = tree_cpu_seconds,
    log_fields: dict[str, Any] | None = None,
) -> None:
    """Until cancelled: judge the chunk every ``check_s`` once its process
    exists; ``report(stalled_s)`` after every judgement."""
    if not policy.enabled:
        return
    fields = log_fields or {}
    began = False
    logged = False
    while True:
        await asyncio.sleep(policy.check_s)
        current = pid()
        if current is None:
            continue  # env phase / not started: nothing to judge
        if not began:
            progress.begin(clock())
            began = True
        reading = await asyncio.to_thread(cpu, current)
        stalled = progress.sample(clock(), reading)
        report(stalled)
        if stalled > 0 and not logged:
            logged = True
            log.warning(
                "agent.lane.chunk_stalled",
                **fields,
                stalled_s=round(stalled, 1),
                pid=current,
                hint="no output and ~no CPU for longer than the stall threshold; "
                "reported to the controller with the lease",
            )
        elif stalled == 0 and logged:
            logged = False
            log.info("agent.lane.chunk_resumed", **fields, pid=current)
