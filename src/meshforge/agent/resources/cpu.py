"""Whole-machine CPU utilisation from our own ``cpu_times()`` deltas (V1.0 G9-C, D435).

Why not ``psutil.cpu_percent(interval=None)``: it answers "busy since the
previous call **by this thread**" - psutil keys its baseline by
``threading.get_ident()``. The resource sampler runs every sample on a fresh
daemon thread (:class:`~meshforge.agent.hardware.deadline.DeadlineThread`, so a
wedged driver call can be abandoned). On Linux glibc reuses the finished
thread's ident, so it worked by accident; on Windows a new thread usually gets
a new id, so each call compared two ``GetSystemTimes`` readings microseconds
apart - noise or a flat ``0.0`` - and grew psutil's dict by one entry per
sample. The likely cause of lab bug #8 (a 12-process burn read 32/0/27/32 %);
to be confirmed on Windows with ``meshforge-agent doctor cpu``.

:class:`CpuMeter` keeps its own baseline (one per meter, any thread may call
it; the sampler's one-slot guard means one call at a time, a lock makes it
safe regardless). A read that fails, a counter that went backwards (Linux
``iowait`` can; a wrapped or reset counter), no measurable time since the
baseline, or a window shorter than ``min_window_s`` (a few clock ticks are
quantisation noise: Windows counts in 15.6 ms ticks) gives ``percent=None``
with a typed ``reason`` - **never 0 %**. What the caller does with an unknown
value is its policy (the sampler reuses the last good value for a bounded time,
flagged degraded).

The arithmetic matches psutil's: total = sum of the fields minus ``guest`` /
``guest_nice`` (Linux already counts them in ``user`` / ``nice``); idle =
``idle`` + ``iowait``; busy = total - idle. Small negative per-field deltas are
clamped to 0 (psutil does the same); a negative *sum* is a reset and rebases.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "CpuMeter",
    "CpuReading",
    "CpuUnknown",
    "busy_percent",
    "core_percents",
]

_EXCLUDED: Final = frozenset({"guest", "guest_nice"})
_IDLE: Final = ("idle", "iowait")


class CpuUnknown(StrEnum):
    """Why a reading has no percentage (``CpuReading.reason``)."""

    NO_BASELINE = "no_baseline"
    """First read of a meter: it only takes the baseline."""
    READ_FAILED = "read_failed"
    """``cpu_times()`` raised (detail: the exception type)."""
    BACKWARDS = "backwards"
    """The counters' sum went down (reset or wrap): rebased."""
    NO_TIME = "no_time"
    """No measurable CPU time since the baseline (two reads inside one tick)."""
    SHORT_WINDOW = "short_window"
    """Less than ``min_window_s`` since the baseline: baseline kept."""


@dataclass(frozen=True, slots=True)
class CpuReading:
    percent: float | None
    """Busy share of the whole machine in [0, 100], or None (see ``reason``)."""
    reason: CpuUnknown | None = None
    detail: str = ""


def _fields(times: Any) -> dict[str, float]:
    as_dict = getattr(times, "_asdict", None)
    if as_dict is None:
        raise TypeError("cpu_times() did not return a named tuple")
    return {k: float(v) for k, v in as_dict().items() if k not in _EXCLUDED}


def busy_percent(before: Any, after: Any) -> CpuReading:
    """Busy share between two ``cpu_times()`` named tuples (pure)."""
    first, second = _fields(before), _fields(after)
    raw = {k: second.get(k, 0.0) - v for k, v in first.items()}
    if sum(raw.values()) < 0:
        return CpuReading(None, CpuUnknown.BACKWARDS)
    deltas = {k: max(0.0, v) for k, v in raw.items()}
    total = sum(deltas.values())
    if not total > 0:
        return CpuReading(None, CpuUnknown.NO_TIME)
    idle = sum(deltas.get(k, 0.0) for k in _IDLE)
    busy = 100.0 * (total - idle) / total
    return CpuReading(min(100.0, max(0.0, busy)))


def core_percents(before: Sequence[Any], after: Sequence[Any]) -> list[float | None]:
    """Per-core busy share between two ``cpu_times(percpu=True)`` lists
    (None for a core whose counters went backwards or did not move)."""
    return [busy_percent(b, a).percent for b, a in zip(before, after, strict=False)]


class CpuMeter:
    """Whole-machine CPU % since this meter's previous good read."""

    __slots__ = (
        "_baseline",
        "_baseline_at",
        "_clock",
        "_lock",
        "_min_window_s",
        "_read",
    )

    def __init__(
        self,
        read: Callable[[], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        min_window_s: float = 0.0,
    ) -> None:
        self._read = read
        self._clock = clock
        self._min_window_s = min_window_s
        self._baseline: Any | None = None
        self._baseline_at = 0.0
        self._lock = threading.Lock()

    def read(self) -> CpuReading:
        """One reading; never raises."""
        with self._lock:
            at = self._clock()
            if (
                self._baseline is not None
                and at - self._baseline_at < self._min_window_s
            ):
                return CpuReading(None, CpuUnknown.SHORT_WINDOW)
            try:
                now = self._read()
                _fields(now)
            except Exception as exc:  # noqa: BLE001 - a failed read is a typed unknown
                return CpuReading(None, CpuUnknown.READ_FAILED, type(exc).__name__)
            before = self._baseline
            if before is None:
                self._baseline, self._baseline_at = now, at
                return CpuReading(None, CpuUnknown.NO_BASELINE)
            reading = busy_percent(before, now)
            if reading.reason is not CpuUnknown.NO_TIME:
                # NO_TIME keeps the older baseline: the next window is longer.
                self._baseline, self._baseline_at = now, at
            return reading
