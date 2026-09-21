"""The resource monitor task (roadmap 0.3.5): sample, decide, report.

One asyncio task, owned by :func:`meshforge.agent.runtime.serve`, next to the
session client. It samples every ``sample_interval_s`` in a worker thread and
decides - with :class:`ReportPolicy`, which is pure and clock-injected - whether the
sample is worth a ``ResourceUpdate``:

* the first sample of each ONLINE session is always reported;
* a sample whose CPU/GPU utilisation moved more than ``change_threshold_percent``
  percentage points, or whose byte metrics moved more than that *relatively*, is
  reported at once;
* otherwise a report goes out at least every ``max_report_interval_s`` (the
  heartbeat-piggyback rule of the brief §2 D5);
* **while the session is not ONLINE nothing is sent at all** (D9). ``send_report``
  buffers into the replay ring, and a ten-minute outage at 2 s would push 300 stale
  samples through it and evict real reports. Sampling continues, so the first sample
  after the session is ONLINE again is fresh - and it is forced through.

**The monitor never takes the agent down.** A sampler exception is logged with its
type, counted, and the loop continues on the next tick; a write that races the
stream ending is swallowed by the session client. The task is referenced and
awaited by ``serve()``; it is not fire-and-forget.

**Nor can a wedged driver stall it** (D10's sibling guarantee). Sampling touches
NVML, which is synchronous FFI with no cancellation point, so every blocking call -
``prime``, ``sample`` and ``close`` - goes through the
:class:`~meshforge.agent.hardware.deadline.DeadlineThread` shared with hardware
discovery: a sample that does not return within ``sample_timeout_s`` is abandoned
and counted, the loop keeps its cadence, and while that one thread is still stuck
further ticks fail fast instead of starting a second one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from meshforge.agent.hardware.deadline import (
    BlockingCallError,
    BlockingCallStalledError,
    DeadlineThread,
)
from meshforge.agent.resources.convert import to_resource_update
from meshforge.agent.resources.sampler import ResourceSample, ResourceSampler
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import ResourceSettings
from meshforge.common.logging import get_logger

__all__ = ["MonitorStats", "ReportPolicy", "ResourceMonitor", "SessionReporter"]

log = get_logger(__name__)

_CLOSE_TIMEOUT_S: Final = 2.0
"""``NvmlSession.close`` is a driver call; shutdown never waits longer than this."""


class SessionReporter(Protocol):
    """What the monitor needs from the session client (G3's ``AgentSessionClient``)."""

    node_id: str
    state: AgentState

    async def send_report(self, payload_name: str, payload: Any) -> str: ...


@dataclass(slots=True)
class MonitorStats:
    """Counters for logs, tests and the future operator surface."""

    samples: int = 0
    reports: int = 0
    suppressed: int = 0
    """Samples taken while ONLINE that were below every reporting trigger."""
    offline_samples: int = 0
    """Samples taken while the session was not ONLINE (D9: never sent)."""
    sampler_errors: int = 0
    sampler_timeouts: int = 0
    """Samples abandoned on their deadline (a wedged NVML/``/proc`` call)."""
    sampler_stalled: int = 0
    """Ticks skipped because an abandoned sample was still running."""
    send_errors: int = 0


def _moved(previous: float, current: float, threshold: float) -> bool:
    """Relative move of a non-negative magnitude, in percent of the previous value."""
    if previous == current:
        return False
    if previous <= 0:
        return current > 0
    return abs(current - previous) / previous * 100.0 > threshold


class ReportPolicy:
    """Pure decision: is this sample worth a report? Holds only the last one sent."""

    __slots__ = ("_force", "_last", "_last_at", "_max_interval_s", "_threshold")

    def __init__(self, *, threshold_percent: float, max_interval_s: float) -> None:
        self._threshold = threshold_percent
        self._max_interval_s = max_interval_s
        self._last: ResourceSample | None = None
        self._last_at: float | None = None
        self._force = True
        """Set at start and whenever the session was not ONLINE (D9)."""

    @property
    def last_reported(self) -> ResourceSample | None:
        return self._last

    def mark_not_online(self) -> None:
        """The session is not usable: the next ONLINE sample must be reported."""
        self._force = True

    def should_report(self, sample: ResourceSample, now: float) -> bool:
        """True when ``sample`` must go out now. Does not mutate the policy."""
        if self._force or self._last is None or self._last_at is None:
            return True
        if now - self._last_at >= self._max_interval_s:
            return True
        return self._changed(self._last, sample)

    def record_reported(self, sample: ResourceSample, now: float) -> None:
        self._last = sample
        self._last_at = now
        self._force = False

    def _changed(self, previous: ResourceSample, current: ResourceSample) -> bool:
        threshold = self._threshold
        if abs(current.cpu_percent - previous.cpu_percent) > threshold:
            return True
        if _moved(previous.ram_used_bytes, current.ram_used_bytes, threshold):
            return True
        if len(previous.gpus) != len(current.gpus):
            return True
        for before, after in zip(previous.gpus, current.gpus, strict=True):
            if abs(after.util_percent - before.util_percent) > threshold:
                return True
            if _moved(before.vram_used_bytes, after.vram_used_bytes, threshold):
                return True
        if [d.mount for d in previous.disks] != [d.mount for d in current.disks]:
            return True
        return any(
            _moved(before.free_bytes, after.free_bytes, threshold)
            for before, after in zip(previous.disks, current.disks, strict=True)
        )


class ResourceMonitor:
    """The sampling task. Construct, then ``await run()`` in exactly one task."""

    def __init__(
        self,
        client: SessionReporter,
        sampler: ResourceSampler,
        settings: ResourceSettings,
        *,
        clock: Callable[[], float] | None = None,
        guard: DeadlineThread | None = None,
    ) -> None:
        self._client = client
        self._sampler = sampler
        self._settings = settings
        self._clock_override = clock
        self._guard = guard if guard is not None else DeadlineThread("resources")
        self._policy = ReportPolicy(
            threshold_percent=settings.change_threshold_percent,
            max_interval_s=settings.max_report_interval_s,
        )
        self._stop = asyncio.Event()
        self._running = False
        self.stats = MonitorStats()
        self.last_sample: ResourceSample | None = None

    @property
    def policy(self) -> ReportPolicy:
        return self._policy

    def request_stop(self) -> None:
        """Ask the loop to finish its current tick and return from :meth:`run`."""
        self._stop.set()

    async def run(self) -> None:
        """Sample and report until :meth:`request_stop`. Never raises for a sample."""
        if self._running:
            raise RuntimeError("ResourceMonitor.run() is already running")
        self._running = True
        loop = asyncio.get_running_loop()
        clock = self._clock_override or loop.time
        interval = self._settings.sample_interval_s
        log.info(
            "agent.resources.started",
            node_id=self._client.node_id,
            sample_interval_s=interval,
            change_threshold_percent=self._settings.change_threshold_percent,
            max_report_interval_s=self._settings.max_report_interval_s,
        )
        await self._blocking(self._sampler.prime, self._settings.sample_timeout_s)
        try:
            while not self._stop.is_set():
                await self._tick(clock())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), interval)
        finally:
            # A wedged sample still owns the NVML session: closing it from here
            # would race that thread, so the close is skipped and logged instead.
            await self._blocking(self._sampler.close, _CLOSE_TIMEOUT_S)
            log.info(
                "agent.resources.stopped",
                node_id=self._client.node_id,
                samples=self.stats.samples,
                reports=self.stats.reports,
                suppressed=self.stats.suppressed,
                sampler_errors=self.stats.sampler_errors,
                sampler_timeouts=self.stats.sampler_timeouts,
                sampler_stalled=self.stats.sampler_stalled,
            )

    async def _tick(self, now: float) -> None:
        sample = await self._sample()
        if sample is None:
            return
        self.stats.samples += 1
        self.last_sample = sample
        if self._client.state is not AgentState.ONLINE:
            # D9: keep sampling, send nothing, and force the next ONLINE sample.
            self.stats.offline_samples += 1
            self._policy.mark_not_online()
            return
        if not self._policy.should_report(sample, now):
            self.stats.suppressed += 1
            return
        await self._send(sample, now)

    async def _sample(self) -> ResourceSample | None:
        try:
            return await self._guard.call(
                self._sampler.sample, timeout_s=self._settings.sample_timeout_s
            )
        except asyncio.CancelledError:
            raise
        except BlockingCallStalledError:
            self.stats.sampler_stalled += 1
            log.debug(
                "agent.resources.sample_stalled",
                node_id=self._client.node_id,
                failure_type="stalled",
                stalled=self.stats.sampler_stalled,
            )
            return None
        except BlockingCallError:
            self.stats.sampler_errors += 1
            self.stats.sampler_timeouts += 1
            log.warning(
                "agent.resources.sample_timeout",
                node_id=self._client.node_id,
                failure_type="timeout",
                timeout_s=self._settings.sample_timeout_s,
                failures=self.stats.sampler_errors,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - one bad sample must not end the loop
            self.stats.sampler_errors += 1
            log.warning(
                "agent.resources.sample_failed",
                node_id=self._client.node_id,
                cause=type(exc).__name__,
                error=str(exc)[:200],
                failures=self.stats.sampler_errors,
            )
            return None

    async def _blocking(self, fn: Callable[[], None], timeout_s: float) -> None:
        """Run one guarded lifecycle call (``prime``/``close``); never raises."""
        try:
            await self._guard.call(fn, timeout_s=timeout_s)
        except asyncio.CancelledError:
            raise
        except BlockingCallError as exc:
            log.warning(
                "agent.resources.blocking_call_skipped",
                node_id=self._client.node_id,
                call=getattr(fn, "__name__", "call"),
                failure_type="timeout",
                reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - lifecycle must not end the agent
            log.warning(
                "agent.resources.blocking_call_failed",
                node_id=self._client.node_id,
                call=getattr(fn, "__name__", "call"),
                cause=type(exc).__name__,
                error=str(exc)[:200],
            )

    async def _send(self, sample: ResourceSample, now: float) -> None:
        update = to_resource_update(
            sample,
            max_gpus=self._settings.max_gpus,
            max_disks=self._settings.max_disks,
        )
        try:
            await self._client.send_report("resource_update", update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed write is the client's problem
            self.stats.send_errors += 1
            log.warning(
                "agent.resources.report_failed",
                node_id=self._client.node_id,
                cause=type(exc).__name__,
                error=str(exc)[:200],
            )
            return
        self.stats.reports += 1
        self._policy.record_reported(sample, now)
        if sample.degraded:
            log.debug(
                "agent.resources.degraded",
                node_id=self._client.node_id,
                reasons=list(sample.degraded),
            )
