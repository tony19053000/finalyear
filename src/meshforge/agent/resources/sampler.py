"""One blocking read of this machine's live resource usage (roadmap 0.3.5).

:class:`ResourceSampler` is the only place that touches ``psutil`` and NVML on the
sampling path. It is **blocking** (``psutil`` reads ``/proc``, NVML calls into the
driver) and is called from :class:`~meshforge.agent.resources.monitor.ResourceMonitor`
through a :class:`~meshforge.agent.hardware.deadline.DeadlineThread`, so the agent's
event loop never waits on a stalled ``/proc`` read or a wedged GPU driver: a sample
that outlives ``agent.resources.sample_timeout_s`` is abandoned and counted.

Two things are deliberately stateful:

* ``psutil.cpu_percent(interval=None)`` reports utilisation *since the previous
  call*, so the first call after start-up is meaningless. :meth:`prime` makes that
  first call and throws the answer away.
* The NVML session keeps its device handles open for the process lifetime (opening
  them per sample costs a driver round trip). If a device call fails, the session
  invalidates itself and the next sample re-acquires - a driver that was reloaded
  comes back without restarting the agent.

The mount list is taken once from :mod:`meshforge.agent.hardware.disks` and reused:
only per-mount usage is re-read each sample.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import dataclass, field
from typing import Final

from meshforge.agent.hardware.disks import DiskFacts, disk_usage, list_mounts
from meshforge.agent.hardware.gpu import GpuUsage, NvmlSession
from meshforge.agent.hardware.limits import MAX_DISKS, MAX_GPUS
from meshforge.agent.hardware.optional import psutil_module

__all__ = ["ResourceSample", "ResourceSampler"]

_PERCENT_MAX: Final = 100.0


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One observation of the node's usage. Percentages are already in ``[0, 100]``."""

    observed_at: dt.datetime
    """Wall clock, for display and for the controller's history buckets only."""
    cpu_percent: float = 0.0
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

    __slots__ = ("_max_disks", "_max_gpus", "_mounts", "_nvml", "_primed")

    def __init__(
        self,
        *,
        mounts: list[str] | None = None,
        nvml: NvmlSession | None = None,
        max_gpus: int = MAX_GPUS,
        max_disks: int = MAX_DISKS,
    ) -> None:
        self._mounts = mounts if mounts is not None else list_mounts()
        self._nvml = nvml if nvml is not None else NvmlSession()
        self._max_gpus = max(1, min(max_gpus, MAX_GPUS))
        self._max_disks = max(1, min(max_disks, MAX_DISKS))
        self._primed = False

    @property
    def mounts(self) -> tuple[str, ...]:
        return tuple(self._mounts)

    def prime(self) -> None:
        """Take and discard the first CPU reading (see the module docstring)."""
        psutil = psutil_module()
        if psutil is not None:
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                psutil.cpu_percent(interval=None)
        self._primed = True

    def close(self) -> None:
        """Release the NVML session. Idempotent."""
        self._nvml.close()

    def sample(self) -> ResourceSample:
        """Read CPU, RAM, GPU and disk usage now. Blocking; never raises."""
        if not self._primed:
            self.prime()
        degraded: list[str] = []
        cpu_percent = 0.0
        ram_used = ram_total = 0
        psutil = psutil_module()
        if psutil is None:
            degraded.append("cpu/ram: psutil is not installed")
        else:
            try:
                cpu_percent = _clamp_percent(float(psutil.cpu_percent(interval=None)))
            except (OSError, RuntimeError, ValueError) as exc:
                degraded.append(f"cpu: {type(exc).__name__}")
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
