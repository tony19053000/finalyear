"""One blocking read of this machine's live resource usage (roadmap 0.3.5).

:class:`ResourceSampler` is the only place that touches ``psutil`` and NVML on the
sampling path. It is **blocking** (``psutil`` reads ``/proc``, NVML calls into the
driver) and is called from :class:`~meshforge.agent.resources.monitor.ResourceMonitor`
through a :class:`~meshforge.agent.hardware.deadline.DeadlineThread`, so the agent's
event loop never waits on a stalled ``/proc`` read or a wedged GPU driver: a sample
that outlives ``agent.resources.sample_timeout_s`` is abandoned and counted.

Two things are deliberately stateful:

* CPU % is the delta of ``psutil.cpu_times()`` since the previous sample, kept by
  our own :class:`~meshforge.agent.resources.cpu.CpuMeter` (V1.0 G9-C, D435) - not
  ``psutil.cpu_percent``, whose baseline is per *thread* and every sample runs on a
  new one. :meth:`prime` takes the first baseline. A CPU read that fails (or has
  no measurable window) reuses the last good value for at most ``max_stale_s``,
  flagged in ``degraded``; with no such value ``cpu_percent`` is ``None``
  (unknown) - **never 0 %**. The monitor does not send a sample with unknown CPU.
* The NVML session keeps its device handles open for the process lifetime (opening
  them per sample costs a driver round trip). If a device call fails, the session
  invalidates itself and the next sample re-acquires - a driver that was reloaded
  comes back without restarting the agent.

The mount list is taken once from :mod:`meshforge.agent.hardware.disks` and reused:
only per-mount usage is re-read each sample.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.agent.hardware.disks import DiskFacts, disk_usage, list_mounts
from meshforge.agent.hardware.gpu import GpuUsage, NvmlSession
from meshforge.agent.hardware.limits import MAX_DISKS, MAX_GPUS
from meshforge.agent.hardware.optional import psutil_module
from meshforge.agent.resources.cpu import CpuMeter, CpuReading

__all__ = ["ResourceSample", "ResourceSampler"]

_PERCENT_MAX: Final = 100.0
DEFAULT_MAX_STALE_S: Final = 30.0
"""How long a failed CPU read may reuse the last good value (D435)."""
DEFAULT_MIN_CPU_WINDOW_S: Final = 0.25
"""A CPU window shorter than this is noise (a few 15.6 ms Windows ticks)."""


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One observation of the node's usage. Percentages are already in ``[0, 100]``."""

    observed_at: dt.datetime
    """Wall clock, for display and for the controller's history buckets only."""
    cpu_percent: float | None = 0.0
    """Whole-machine CPU %; None = unknown (no good read within ``max_stale_s``)."""
    ram_used_bytes: int = 0
    ram_total_bytes: int = 0
    gpus: tuple[GpuUsage, ...] = ()
    disks: tuple[DiskFacts, ...] = ()
    degraded: tuple[str, ...] = field(default=())
    """Short reasons for fields this sample could not fill (``"gpu: ..."``)."""


def _clamp_percent(value: float) -> float:
    if value != value:  # NaN from a driver that reported nothing
        return 0.0
    return min(max(value, 0.0), _PERCENT_MAX)


class ResourceSampler:
    """Blocking sampler; construct once per agent process and :meth:`close` at exit."""

    __slots__ = (
        "_clock",
        "_cpu",
        "_last_cpu",
        "_max_disks",
        "_max_gpus",
        "_max_stale_s",
        "_mounts",
        "_nvml",
        "_primed",
    )

    def __init__(
        self,
        *,
        mounts: list[str] | None = None,
        nvml: NvmlSession | None = None,
        max_gpus: int = MAX_GPUS,
        max_disks: int = MAX_DISKS,
        cpu_times: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_stale_s: float = DEFAULT_MAX_STALE_S,
        min_cpu_window_s: float = DEFAULT_MIN_CPU_WINDOW_S,
    ) -> None:
        """``cpu_times`` (tests): the ``psutil.cpu_times`` stand-in; ``clock``:
        monotonic seconds, for the age of the last good CPU value."""
        self._mounts = mounts if mounts is not None else list_mounts()
        self._nvml = nvml if nvml is not None else NvmlSession()
        self._max_gpus = max(1, min(max_gpus, MAX_GPUS))
        self._max_disks = max(1, min(max_disks, MAX_DISKS))
        self._primed = False
        self._cpu = CpuMeter(
            cpu_times if cpu_times is not None else _psutil_cpu_times,
            clock=clock,
            min_window_s=min_cpu_window_s,
        )
        self._clock = clock
        self._max_stale_s = max_stale_s
        self._last_cpu: tuple[float, float] | None = None
        """(percent, monotonic time) of the last good CPU reading."""

    @property
    def mounts(self) -> tuple[str, ...]:
        return tuple(self._mounts)

    def prime(self) -> None:
        """Take the first CPU baseline (see the module docstring)."""
        self._cpu.read()
        self._primed = True

    def close(self) -> None:
        """Release the NVML session. Idempotent."""
        self._nvml.close()

    def sample(self) -> ResourceSample:
        """Read CPU, RAM, GPU and disk usage now. Blocking; never raises."""
        if not self._primed:
            self.prime()
        degraded: list[str] = []
        cpu_percent = self._cpu_percent(self._cpu.read(), degraded)
        ram_used = ram_total = 0
        psutil = psutil_module()
        if psutil is None:
            degraded.append("ram: psutil is not installed")
        else:
            try:
                memory = psutil.virtual_memory()
                ram_used, ram_total = int(memory.used), int(memory.total)
            except (OSError, RuntimeError, ValueError) as exc:
                degraded.append(f"ram: {type(exc).__name__}")

        gpus = tuple(self._nvml.usage())[: self._max_gpus]
        if not gpus and self._nvml.reason:
            degraded.append(f"gpu: {self._nvml.reason}")
        gpus = tuple(
            GpuUsage(
                index=gpu.index,
                util_percent=_clamp_percent(gpu.util_percent),
                vram_used_bytes=max(gpu.vram_used_bytes, 0),
                vram_total_bytes=max(gpu.vram_total_bytes, 0),
            )
            for gpu in gpus
        )
        disks = tuple(disk_usage(self._mounts))[: self._max_disks]
        return ResourceSample(
            observed_at=dt.datetime.now(dt.UTC),
            cpu_percent=cpu_percent,
            ram_used_bytes=ram_used,
            ram_total_bytes=ram_total,
            gpus=gpus,
            disks=disks,
            degraded=tuple(degraded),
        )

    def _cpu_percent(self, reading: CpuReading, degraded: list[str]) -> float | None:
        now = self._clock()
        if reading.percent is not None:
            percent = _clamp_percent(reading.percent)
            self._last_cpu = (percent, now)
            return percent
        why = reading.reason.value if reading.reason is not None else "unknown"
        if reading.detail:
            why = f"{why} {reading.detail}"
        last = self._last_cpu
        if last is not None and now - last[1] <= self._max_stale_s:
            degraded.append(f"cpu: stale ({why})")
            return last[0]
        degraded.append(f"cpu: unknown ({why})")
        return None


def _psutil_cpu_times() -> Any:
    psutil = psutil_module()
    if psutil is None:
        raise RuntimeError("psutil is not installed")
    return psutil.cpu_times()
