"""The scoring runner: probe -> plan -> cases in child processes -> :class:`ScoreSet`.

Brief §5g D32b. Everything is measured in ``python -m meshforge.agent.benchmarks
.child`` processes driven over asyncio pipes, so the agent's event loop is never
blocked and the agent never imports torch or numpy. The guarantees:

* **Bounded.** Each case (setup + warmup + repetitions) has ``case_timeout_s``; the
  whole run has ``budget_s``. A child that does not answer in time is killed with
  its whole process tree (``killpg`` of its own session on POSIX, its kill-on-close
  job object on Windows - :mod:`meshforge.agent.process`, which also makes it die
  with a killed agent) and the case becomes ``FAILED: timeout`` - or ``budget_exhausted``
  when it was the run budget that ran out. Cases the budget never reached are
  ``FAILED: budget_exhausted`` too; nothing is silently omitted.
* **Never raises** out of :meth:`ScoreRunner.run` for a measurement problem: a
  crashed child, garbage on its stdout, a CUDA error, a non-finite sample - each is
  a typed metric status and the run continues with a fresh child. Only
  cancellation (``asyncio`` cancel, Ctrl-C through ``asyncio.run``) propagates, and
  the child tree is killed on that path as well, before the exception leaves.
* **Polite.** The child runs at lowered priority (``nice 10`` /
  ``BELOW_NORMAL_PRIORITY_CLASS``) so a saturating all-threads benchmark cannot
  starve the agent's heartbeats; BLAS thread variables and
  ``CUDA_DEVICE_ORDER=PCI_BUS_ID`` are set in its environment before it imports
  anything (BLAS reads them at import time; D32's device-index rule needs the
  latter).
* **Honest conditions** (D36): battery state and the machine's CPU load in the
  ~2 s before the first case are recorded, never used to refuse.

Child processes: the probe shares the first (one-thread) child with the 1-thread
CPU case; the all-thread cases get a second child; GPU cases a third. A fatal error
(timeout, crash, protocol garbage) kills the current child and the next case starts
a new one.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import statistics
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from meshforge.agent.benchmarks import scores
from meshforge.agent.benchmarks.childproc import (
    CHILD_MODULE,
    THREAD_VARS,
    ChildError,
    ChildProcess,
    thread_env,
)
from meshforge.agent.benchmarks.model import (
    GPU_METRICS,
    HOST_METRICS,
    Conditions,
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
    ScoreSet,
)
from meshforge.agent.benchmarks.plan import (
    Case,
    GpuRef,
    Targets,
    names_match,
    plan_cases,
)
from meshforge.agent.hardware.optional import psutil_module
from meshforge.agent.hardware.probe import sanitize_reason
from meshforge.agent.process import NICE, kill_tree
from meshforge.common.ids import new_score_run_id
from meshforge.common.logging import get_logger

__all__ = [
    "BACKGROUND_LOAD_THRESHOLD",
    "CHILD_MODULE",
    "LOAD_WINDOW_S",
    "NICE",
    "THREAD_VARS",
    "ChildError",
    "ChildProcess",
    "HostFacts",
    "RunnerConfig",
    "ScoreRunner",
    "kill_tree",
    "measure_conditions",
    "thread_env",
]

log = get_logger(__name__)

LOAD_WINDOW_S: Final = 2.0
"""The window before the first case over which the machine's CPU load is measured."""
BACKGROUND_LOAD_THRESHOLD: Final = 25.0
"""``background_load_high`` when the load before the run exceeds this (percent)."""

_MIN_TIMEOUT_S: Final = 0.05

#: Child failure codes that mean "this machine cannot measure it" (UNAVAILABLE).
_UNAVAILABLE: Final[dict[str, MetricReason]] = {
    "no_blas_backend": MetricReason.NO_BLAS_BACKEND,
    "torch_not_installed": MetricReason.TORCH_NOT_INSTALLED,
    "cuda_unavailable": MetricReason.CUDA_UNAVAILABLE,
    "cuda_broken": MetricReason.CUDA_UNAVAILABLE,
    "unsupported_dtype": MetricReason.UNSUPPORTED_DTYPE,
}
#: Child failure codes kept as their own FAILED reason; anything else is benchmark_error.
_FAILED: Final[dict[str, MetricReason]] = {
    "device_mismatch": MetricReason.DEVICE_MISMATCH,
    "insufficient_vram": MetricReason.INSUFFICIENT_VRAM,
    "gpu_setup_failed": MetricReason.GPU_SETUP_FAILED,
    "gpu_rep_failed": MetricReason.GPU_REP_FAILED,
}


# -------------------------------------------------------------- conditions
type ConditionsProbe = Callable[[], Awaitable[tuple[bool | None, float | None]]]


async def measure_conditions() -> tuple[bool | None, float | None]:
    """``(on_battery, cpu_load_percent)`` over :data:`LOAD_WINDOW_S`; None = unknown.

    Uses ``psutil`` (agent extra) without blocking the loop: two ``cpu_times()``
    readings around an ``asyncio.sleep``.
    """
    psutil = psutil_module()
    if psutil is None:
        return None, None
    on_battery: bool | None = None
    with contextlib.suppress(Exception):
        battery = psutil.sensors_battery()
        if battery is not None and battery.power_plugged is not None:
            on_battery = not bool(battery.power_plugged)
    try:
        before = psutil.cpu_times()
        await asyncio.sleep(LOAD_WINDOW_S)
        after = psutil.cpu_times()
    except (OSError, RuntimeError, AttributeError):
        return on_battery, None
    return on_battery, busy_percent(before, after)


def busy_percent(before: Any, after: Any) -> float | None:
    """Machine-wide busy share between two ``psutil.cpu_times()`` readings."""

    def idle(t: Any) -> float:
        return float(getattr(t, "idle", 0.0)) + float(getattr(t, "iowait", 0.0))

    total = float(sum(after)) - float(sum(before))
    if not total > 0:
        return None
    busy = 100.0 * (1.0 - (idle(after) - idle(before)) / total)
    return min(100.0, max(0.0, busy))


# -------------------------------------------------------------------- runner
@dataclass(frozen=True, slots=True)
class HostFacts:
    """What the plan needs from discovery (``HardwareFacts``)."""

    cpu_threads: int
    gpus: tuple[GpuRef, ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    budget_s: float = 90.0
    case_timeout_s: float = 30.0
    lower_priority: bool = True
    targets: Targets = field(default_factory=Targets)
    python: str = field(default_factory=lambda: sys.executable)
    extra_env: Mapping[str, str] = field(default_factory=dict)


def _available_ram() -> int | None:
    psutil = psutil_module()
    if psutil is None:
        return None
    try:
        return int(psutil.virtual_memory().available)
    except (OSError, RuntimeError, AttributeError, ValueError):
        return None


class ScoreRunner:
    """One scoring run. Construct, then ``await run()`` once (or :meth:`run_sync`)."""

    def __init__(
        self,
        config: RunnerConfig,
        host: HostFacts,
        *,
        fingerprint: str,
        conditions: ConditionsProbe = measure_conditions,
        available_ram: Callable[[], int | None] = _available_ram,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        log_fields: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.fingerprint = fingerprint
        self._conditions = conditions
        self._available_ram = available_ram
        self._clock = clock
        self._wall = wall
        self._log = dict(log_fields or {})
        self.spawned_pids: list[int] = []
        """Every child pid this run started (tests check none survives)."""
        self.priority_lowered = False
        self._child: ChildProcess | None = None
        self._deadline = 0.0

    # ------------------------------------------------------------- public API
    def run_sync(self) -> ScoreSet:
        """Blocking entry for the CLI. Ctrl-C cancels the run (child killed) and
        re-raises ``KeyboardInterrupt``."""
        return asyncio.run(self.run())

    async def run(self) -> ScoreSet:
        """Measure this machine. Raises only ``CancelledError`` (child killed first)."""
        run_id = new_score_run_id()
        started = self._clock()
        self._deadline = started + self.config.budget_s
        measured_at = self._wall()
        log.info(
            "agent.scores.run_started",
            run_id=run_id,
            **self._log,
            budget_s=self.config.budget_s,
        )
        try:
            on_battery, load = await self._measure_conditions()
            caps = await self._probe()
            plan = plan_cases(
                caps,
                gpus=self.host.gpus,
                cpu_threads=self.host.cpu_threads,
                available_ram_bytes=self._available_ram(),
                targets=self.config.targets,
            )
            measured = [await self._run_case(case) for case in plan.cases]
        except BaseException:
            # Cancellation / Ctrl-C: no polite "bye" (the child is mid-case and
            # would not read it) - the tree dies *now*, then it is reaped.
            child, self._child = self._child, None
            if child is not None:
                child.kill_now()
                await child.reap()
            log.info("agent.scores.run_cancelled", run_id=run_id, **self._log)
            raise
        await self._drop_child()
        duration = max(0.0, self._clock() - started)
        metrics = _ordered([*plan.resolved, *measured], self.host.gpus)
        exhausted = any(m.reason is MetricReason.BUDGET_EXHAUSTED for m in metrics)
        result = ScoreSet(
            run_id=run_id,
            measured_at=measured_at,
            score_version=scores.SCORE_VERSION,
            fingerprint=self.fingerprint,
            conditions=Conditions(
                on_battery=on_battery,
                cpu_load_percent_before=None if load is None else round(load, 1),
                background_load_high=None
                if load is None
                else load > BACKGROUND_LOAD_THRESHOLD,
                priority_lowered=self.priority_lowered,
                budget_s=self.config.budget_s,
                duration_s=round(duration, 3),
                budget_exhausted=exhausted,
            ),
            metrics=tuple(metrics),
        )
        log.info(
            "agent.scores.run_finished",
            run_id=run_id,
            **self._log,
            duration_s=round(duration, 3),
            statuses=result.statuses(),
        )
        return result

    # ------------------------------------------------------------- internals
    def _remaining(self) -> float:
        return self._deadline - self._clock()

    async def _measure_conditions(self) -> tuple[bool | None, float | None]:
        try:
            return await asyncio.wait_for(
                self._conditions(),
                max(_MIN_TIMEOUT_S, min(LOAD_WINDOW_S * 3, self._remaining())),
            )
        except TimeoutError:
            return None, None
        except Exception as exc:  # noqa: BLE001 - conditions are best effort
            log.warning(
                "agent.scores.conditions_failed", **self._log, cause=type(exc).__name__
            )
            return None, None

    async def _child_for(self, threads: int) -> ChildProcess:
        child = self._child
        if child is not None and not child.dead and child.threads == threads:
            return child
        await self._drop_child()
        child = ChildProcess(
            threads=threads,
            lower_priority=self.config.lower_priority,
            python=self.config.python,
            extra_env=self.config.extra_env,
            on_spawn=self.spawned_pids.append,
        )
        self._child = child
        await child.start()
        return child

    async def _drop_child(self) -> None:
        child, self._child = self._child, None
        if child is None:
            return
        try:
            await child.close()
        except BaseException:
            child.kill_now()
            raise

    async def _probe(self) -> dict[str, Any]:
        timeout = min(self.config.case_timeout_s, self._remaining())
        if timeout <= 0:
            return {
                "probe_failure": {
                    "reason": MetricReason.BUDGET_EXHAUSTED.value,
                    "detail": "no budget left for the probe",
                }
            }
        try:
            child = await self._child_for(1)
            info = await child.call({"op": "probe"}, "info", timeout)
        except ChildError as exc:
            reason = self._fatal_reason(exc)
            log.warning(
                "agent.scores.probe_failed",
                **self._log,
                failure_type=reason.value,
                detail=exc.detail,
            )
            return {"probe_failure": {"reason": reason.value, "detail": exc.detail}}
        priority = info.get("priority")
        self.priority_lowered = bool(
            isinstance(priority, dict) and priority.get("lowered")
        )
        return info

    def _fatal_reason(self, exc: ChildError) -> MetricReason:
        if exc.reason is MetricReason.TIMEOUT and self._remaining() <= 0:
            return MetricReason.BUDGET_EXHAUSTED
        return exc.reason or MetricReason.BENCHMARK_ERROR

    async def _run_case(self, case: Case) -> Metric:
        if self._remaining() <= 0:
            return _not_measured(
                case,
                MetricStatus.FAILED,
                MetricReason.BUDGET_EXHAUSTED,
                "not started: the run budget ran out",
            )
        case_deadline = min(self._clock() + self.config.case_timeout_s, self._deadline)
        targets = self.config.targets
        try:
            child = await self._child_for(case.threads)
            info = await child.call(
                {"op": "setup", "spec": case.spec},
                "info",
                case_deadline - self._clock(),
            )
            mismatch = self._setup_mismatch(case, info)
            if mismatch is not None:
                return _not_measured(
                    case, MetricStatus.FAILED, MetricReason.DEVICE_MISMATCH, mismatch
                )
            samples: list[dict[str, Any]] = []
            for index in range(targets.warmup + targets.reps):
                sample = await child.call(
                    {"op": "rep"}, "sample", case_deadline - self._clock()
                )
                if index >= targets.warmup:
                    samples.append(sample)
        except ChildError as exc:
            metric = self._failure(case, exc)
            log.warning(
                "agent.scores.case_failed",
                **self._log,
                case=case.key,
                status=metric.status.value,
                failure_type=metric.reason.value if metric.reason else None,
                detail=metric.detail,
            )
            return metric
        return _summarise(case, info, samples)

    def _setup_mismatch(self, case: Case, info: Mapping[str, Any]) -> str | None:
        """Second check of D32's rule, on the device the case really opened."""
        if case.gpu_index is None:
            return None
        gpu = next((g for g in self.host.gpus if g.index == case.gpu_index), None)
        name = str(info.get("device_name", ""))
        if gpu is None or not names_match(gpu.model, name):
            return (
                f"case opened torch device {case.gpu_index} = {name or '(unknown)'!r}, "
                f"NVML says {gpu.model if gpu else '(no such GPU)'!r}"
            )
        return None

    def _failure(self, case: Case, exc: ChildError) -> Metric:
        if exc.fatal or exc.reason is not None:
            reason = self._fatal_reason(exc)
            return _not_measured(case, MetricStatus.FAILED, reason, exc.detail)
        if exc.code in _UNAVAILABLE:
            return _not_measured(
                case, MetricStatus.UNAVAILABLE, _UNAVAILABLE[exc.code], exc.detail
            )
        reason = _FAILED.get(exc.code, MetricReason.BENCHMARK_ERROR)
        detail = (
            exc.detail
            if reason is not MetricReason.BENCHMARK_ERROR
            else (f"{exc.code}: {exc.detail}")
        )
        return _not_measured(case, MetricStatus.FAILED, reason, detail)


# ------------------------------------------------------------------ helpers
def _not_measured(
    case: Case, status: MetricStatus, reason: MetricReason, detail: str
) -> Metric:
    return Metric.not_measured(
        case.metric, status, reason, sanitize_reason(detail), gpu_index=case.gpu_index
    )


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _summarise(
    case: Case, info: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]
) -> Metric:
    values = [_finite_positive(s.get("value")) for s in samples]
    elapsed = [_finite_positive(s.get("elapsed_s")) for s in samples]
    good = [v for v in values if v is not None]
    times = [t for t in elapsed if t is not None]
    if len(good) != len(samples) or len(times) != len(samples):
        return _not_measured(
            case,
            MetricStatus.FAILED,
            MetricReason.BAD_SAMPLE,
            "a repetition returned a missing, non-finite or non-positive value",
        )
    stab = scores.stability(times)
    threads = info.get("threads_effective")
    return Metric(
        name=case.metric,
        gpu_index=case.gpu_index,
        status=MetricStatus.OK,
        mean=statistics.fmean(good),
        stdev=statistics.stdev(good),
        n=len(good),
        backend=_short(info.get("backend")),
        backend_version=_short(info.get("backend_version")),
        threads_effective=threads
        if isinstance(threads, int) and threads >= 1
        else None,
        lower_bound=bool(info.get("lower_bound", False)),
        throttling_suspected=None
        if stab["inconclusive"]
        else bool(stab["thermal_throttling_suspected"]),
    )


def _short(value: object) -> str | None:
    if value is None:
        return None
    return sanitize_reason(str(value))[:64] or None


def _ordered(metrics: Sequence[Metric], gpus: Sequence[GpuRef]) -> list[Metric]:
    """Canonical order: host metrics, then per GPU (NVML index order)."""
    order: dict[tuple[MetricName, int | None], int] = {}
    for name in HOST_METRICS:
        order[(name, None)] = len(order)
    for gpu in sorted(gpus, key=lambda g: g.index):
        for name in GPU_METRICS:
            order[(name, gpu.index)] = len(order)
    return sorted(metrics, key=lambda m: order.get((m.name, m.gpu_index), len(order)))
