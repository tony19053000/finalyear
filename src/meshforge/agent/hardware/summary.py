"""``discover()``: one blocking pass over every probe, cached by :class:`HardwareProvider`.

Roadmap 0.3.3. The agent calls this **once**, at start-up, through
``asyncio.to_thread`` (every probe is blocking by design - ``statvfs``, NVML,
subprocesses), and puts the resulting :class:`~...agent_pb2.HardwareSummary` in its
Hello. :class:`HardwareProvider` holds the result for the process and offers
``refresh()`` so 0.3.4's hardware scores (G7) and a future on-demand re-probe reuse
the same cache instead of inventing a second one.

Two shapes come out of one pass:

* :class:`HardwareFacts` - the rich local record (disks, per-probe status, reason
  and duration). Nothing sends it yet; G5's REST and G7's scores want it.
* ``HardwareSummary`` (:meth:`HardwareFacts.to_summary`) - the wire message, already
  clipped to every cap in :mod:`meshforge.agent.hardware.limits` (D12), so the
  controller never has to reject our own Hello.

**Nothing in this module raises** (D10): the probes are individually guarded and a
probe that fails, times out or is not applicable costs its own field. A machine
where *everything* fails still yields a valid summary with the CPU basics
``os.cpu_count()`` can always supply.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.agent.hardware.cpu import CpuFacts, probe_cpu
from meshforge.agent.hardware.disks import DiskFacts, probe_disks
from meshforge.agent.hardware.gpu import GpuFacts, NvmlSession, probe_gpus
from meshforge.agent.hardware.limits import clip_hardware
from meshforge.agent.hardware.probe import Budget, ProbeOutcome
from meshforge.agent.hardware.software import probe_software
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "SHAPE_CAPABILITY",
    "HardwareFacts",
    "HardwareProvider",
    "discover",
]

log = get_logger(__name__)

DEFAULT_TIMEOUT_S: Final = 5.0
"""Default total budget of one ``discover()`` pass (``agent.hardware.timeout_s``)."""

SHAPE_CAPABILITY: Final = "v0.3"
"""First capability tag; G3 documented it as the marker of the Hello shape."""


@dataclass(frozen=True, slots=True)
class HardwareFacts:
    """Everything one discovery pass learned, including what it could not learn."""

    cpu: CpuFacts
    gpus: tuple[GpuFacts, ...] = ()
    disks: tuple[DiskFacts, ...] = ()
    capabilities: tuple[str, ...] = ()
    probes: tuple[ProbeOutcome[Any], ...] = ()
    discovered_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    """Wall clock, for display only; the budget itself is monotonic."""
    duration_s: float = 0.0

    @property
    def degraded(self) -> tuple[str, ...]:
        """``"gpu: timeout: ..."`` for every probe that is not ``OK`` (logs, REST)."""
        return tuple(
            f"{outcome.name}: {outcome.status.value}: {outcome.reason}".rstrip(": ")
            for outcome in self.probes
            if not outcome.ok
        )

    def to_summary(self) -> agent_pb2.HardwareSummary:
        """The ``Hello.hardware`` message, clipped to every D12 cap."""
        summary = agent_pb2.HardwareSummary(
            cpu_model=self.cpu.model,
            cpu_cores=max(self.cpu.physical_cores, 0),
            cpu_threads=max(self.cpu.logical_threads, 0),
            ram_bytes=max(self.cpu.ram_bytes, 0),
            capabilities=[SHAPE_CAPABILITY, *self.capabilities],
        )
        for gpu in self.gpus:
            summary.gpus.append(
                agent_pb2.HardwareSummary.Gpu(
                    index=gpu.index,
                    model=gpu.model,
                    vram_bytes=gpu.vram_bytes,
                    driver_version=gpu.driver_version,
                    compute_capability=gpu.compute_capability,
                )
            )
        return clip_hardware(summary)


def discover(
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    nvml: NvmlSession | None = None,
) -> HardwareFacts:
    """Probe this machine once. Blocking; never raises; see the module docstring.

    ``nvml`` lets the resource monitor share its already-open NVML session instead
    of initialising the library twice.
    """
    budget = Budget(timeout_s, clock=clock)
    started = budget.now()
    cpu = probe_cpu(budget)
    disks = probe_disks(budget)
    gpus = probe_gpus(budget, session=nvml)
    software = probe_software(budget, has_gpu=bool(gpus.value))
    facts = HardwareFacts(
        cpu=cpu.value or _fallback_cpu(),
        gpus=tuple(gpus.value or ()),
        disks=tuple(disks.value or ()),
        capabilities=tuple(software.value or ()),
        probes=(cpu, disks, gpus, software),
        duration_s=budget.now() - started,
    )
    log.info(
        "agent.hardware.discovered",
        cpu_threads=facts.cpu.logical_threads,
        gpu_count=len(facts.gpus),
        disk_count=len(facts.disks),
        capabilities=list(facts.capabilities),
        degraded=list(facts.degraded),
        duration_s=round(facts.duration_s, 3),
    )
    return facts


def _fallback_cpu() -> CpuFacts:
    """Used only if the CPU probe itself failed: what the standard library knows."""
    threads = os.cpu_count() or 0
    return CpuFacts(
        model="",
        physical_cores=threads,
        logical_threads=threads,
        ram_bytes=0,
        source="fallback",
    )


class HardwareProvider:
    """Process-wide cache of one :func:`discover` pass.

    Construct once per agent process; ``get()`` probes on first use and returns the
    cached facts afterwards, ``refresh()`` forces a new pass (0.3.4/G7 and a future
    on-demand re-probe). Both are blocking - call them from ``asyncio.to_thread``.
    """

    __slots__ = ("_facts", "_nvml", "_timeout_s")

    def __init__(
        self, *, timeout_s: float = DEFAULT_TIMEOUT_S, nvml: NvmlSession | None = None
    ) -> None:
        self._timeout_s = timeout_s
        self._nvml = nvml
        self._facts: HardwareFacts | None = None

    @property
    def cached(self) -> HardwareFacts | None:
        """The last pass's facts without probing (None before the first ``get()``)."""
        return self._facts

    def get(self) -> HardwareFacts:
        if self._facts is None:
            self._facts = discover(timeout_s=self._timeout_s, nvml=self._nvml)
        return self._facts

    def refresh(self) -> HardwareFacts:
        self._facts = discover(timeout_s=self._timeout_s, nvml=self._nvml)
        return self._facts
