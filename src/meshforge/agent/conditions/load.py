"""``cpu_load:N`` and ``gpu_load:P`` - background load generators (V0.9 G4, D217).

**cpu_load:N** starts N burner processes (:mod:`.burn`), children of the agent
and therefore **outside the job's cgroup / Job Object**, each at the lowest OS
priority. Verified: over ``verify_window_s`` (2 s) the burners' own CPU time
must add up to >= :data:`CPU_VERIFY_FRACTION` x N cores (``measured.load_cores``);
less (a node too busy, or a burner that died) fails the condition.

**gpu_load:P** starts one burner that runs a CUDA matmul loop on the
attempt's GPU (the first of its ``gpu_indices``, else device 0) for P % of
every 100 ms. Verified: the device's NVML utilisation over the window must be
within :data:`GPU_VERIFY_TOLERANCE` points of P (``measured.gpu_util_percent``).

Both are **bounded**: each burner exits by itself at ``deadline_s`` (the
attempt's time limit + slack) and when the agent is gone; :meth:`revert`
kills them (terminate, then kill after 2 s); :meth:`broken` reports a burner
that ended before the attempt did. The pids (with creation times) are in the
marker before revert can be needed, so an agent crash is undone at the next
start.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Final

import psutil  # type: ignore[import-untyped]

from meshforge.agent.conditions.base import (
    ConditionController,
    ConditionFailedError,
    kill_and_reap,
)
from meshforge.agent.conditions.marker import BURNER_MODULE
from meshforge.common.logging import get_logger

__all__ = [
    "CPU_VERIFY_FRACTION",
    "GPU_VERIFY_TOLERANCE",
    "CpuLoadController",
    "GpuLoadController",
    "LoadProcesses",
]

log = get_logger(__name__)

CPU_VERIFY_FRACTION: Final = 0.8
GPU_VERIFY_TOLERANCE: Final = 15.0
_START_SETTLE_S: Final = 0.3
"""Burners need a moment to start (interpreter start-up) before the window."""
_KILL_GRACE_S: Final = 2.0

type UtilReader = Callable[[int], float | None]
"""NVML utilisation (percent) of device ``index``; None = not readable."""


def _creation_flags() -> int:
    if os.name == "nt":  # pragma: no cover - Windows
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


class LoadProcesses:
    """A set of burner processes (shared by both load kinds)."""

    def __init__(
        self,
        mode: str,
        *,
        deadline_s: float,
        on_change: Callable[[], None],
        percent: int = 100,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._mode = mode
        self._deadline_s = deadline_s
        self._percent = percent
        self._env = None if env is None else {**os.environ, **env}
        self._on_change = on_change
        self.procs: list[asyncio.subprocess.Process] = []
        self.identities: list[tuple[int, float]] = []
        """(pid, creation time) of every burner - the marker's entry."""
        self.reverted = False

    async def start(self, count: int) -> None:

        for _ in range(count):
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                BURNER_MODULE,
                self._mode,
                "--deadline-s",
                f"{self._deadline_s:.1f}",
                "--parent",
                str(os.getpid()),
                "--percent",
                str(self._percent),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._env,
                creationflags=_creation_flags(),
            )
            self.procs.append(proc)
            try:
                created = psutil.Process(proc.pid).create_time()
            except psutil.Error:
                created = 0.0  # already gone: verify will say so
            self.identities.append((proc.pid, created))
            self._on_change()

    def cpu_seconds(self) -> float:
        """CPU time (user + system) the live burners used so far."""

        total = 0.0
        for pid, _created in self.identities:
            with contextlib.suppress(psutil.Error):
                times = psutil.Process(pid).cpu_times()
                total += float(times.user) + float(times.system)
        return total

    def ended(self) -> int:
        """Burners that are no longer running."""
        return sum(1 for p in self.procs if p.returncode is not None)

    async def stop(self) -> bool:
        """Terminate, then kill; True = every burner is gone."""
        if self.reverted:
            return True
        for proc in self.procs:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
        gone = True
        for proc in self.procs:
            try:
                await asyncio.wait_for(proc.wait(), _KILL_GRACE_S)
            except TimeoutError:
                if not await kill_and_reap(proc, timeout_s=_KILL_GRACE_S):
                    gone = False
        self.reverted = gone
        return gone


class CpuLoadController(ConditionController):
    kind: ClassVar[str] = "cpu_load"

    def __init__(
        self,
        cores: int,
        *,
        deadline_s: float,
        window_s: float,
        on_change: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.cores = cores
        self._window_s = window_s
        self._clock = clock
        self.load = LoadProcesses("cpu", deadline_s=deadline_s, on_change=on_change)

    def marker_entry(self) -> Mapping[str, Any]:
        return {"pids": [list(i) for i in self.load.identities]}

    async def apply(self) -> None:
        await self.load.start(self.cores)
        await asyncio.sleep(_START_SETTLE_S)
        before, started = self.load.cpu_seconds(), self._clock()
        await asyncio.sleep(self._window_s)
        used, elapsed = self.load.cpu_seconds() - before, self._clock() - started
        load = used / elapsed if elapsed > 0 else 0.0
        self.measured = {
            "cores": self.cores,
            "load_cores": round(load, 3),
            "window_s": round(elapsed, 3),
        }
        if self.load.ended():
            raise ConditionFailedError(
                self.kind, f"{self.load.ended()} load process(es) exited at start"
            )
        if load < CPU_VERIFY_FRACTION * self.cores:
            raise ConditionFailedError(
                self.kind,
                f"measured {load:.2f} busy cores of {self.cores} (needs >= "
                f"{CPU_VERIFY_FRACTION:g} x {self.cores}): the node is too busy",
            )

    def broken(self) -> str | None:
        ended = self.load.ended()
        return f"{ended} load process(es) ended before the attempt" if ended else None

    async def revert(self) -> bool:
        return await self.load.stop()


class GpuLoadController(ConditionController):
    kind: ClassVar[str] = "gpu_load"

    def __init__(
        self,
        percent: int,
        *,
        gpu_index: int,
        deadline_s: float,
        window_s: float,
        util: UtilReader,
        on_change: Callable[[], None],
    ) -> None:
        super().__init__()
        self.percent = percent
        self.gpu_index = gpu_index
        self._window_s = window_s
        self._util = util
        # NVML index == CUDA index under PCI bus order; the burner sees one device
        self.load = LoadProcesses(
            "gpu",
            deadline_s=deadline_s,
            on_change=on_change,
            percent=percent,
            env={
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                "CUDA_VISIBLE_DEVICES": str(gpu_index),
            },
        )

    def marker_entry(self) -> Mapping[str, Any]:
        return {"pids": [list(i) for i in self.load.identities]}

    async def apply(self) -> None:
        await self.load.start(1)
        await asyncio.sleep(_START_SETTLE_S + 2.0)  # CUDA context + first kernels
        samples: list[float] = []
        steps = max(1, round(self._window_s / 0.25))
        for _ in range(steps):
            await asyncio.sleep(self._window_s / steps)
            value = await asyncio.to_thread(self._util, self.gpu_index)
            if value is not None:
                samples.append(value)
        util = sum(samples) / len(samples) if samples else None
        self.measured = {
            "percent": self.percent,
            "gpu_index": self.gpu_index,
            "gpu_util_percent": None if util is None else round(util, 1),
            "samples": len(samples),
        }
        if self.load.ended():
            raise ConditionFailedError(
                self.kind, "the GPU load process exited (no torch / CUDA?)"
            )
        if util is None:
            raise ConditionFailedError(self.kind, "NVML utilisation is not readable")
        if abs(util - self.percent) > GPU_VERIFY_TOLERANCE:
            raise ConditionFailedError(
                self.kind,
                f"measured {util:.0f} % GPU utilisation, wanted {self.percent} "
                f"+- {GPU_VERIFY_TOLERANCE:g}",
            )

    def broken(self) -> str | None:
        return (
            "the GPU load process ended before the attempt"
            if self.load.ended()
            else None
        )

    async def revert(self) -> bool:
        return await self.load.stop()
