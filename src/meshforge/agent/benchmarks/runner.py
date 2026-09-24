"""The scoring runner: probe -> plan -> cases in child processes -> :class:`ScoreSet`.

Brief §5g D32b. Everything is measured in ``python -m meshforge.agent.benchmarks
.child`` processes driven over asyncio pipes, so the agent's event loop is never
blocked and the agent never imports torch or numpy. The guarantees:

* **Bounded.** Each case (setup + warmup + repetitions) has ``case_timeout_s``; the
  whole run has ``budget_s``. A child that does not answer in time is killed with
  its whole process tree (``killpg`` of its own session on POSIX, ``taskkill /T``
  on Windows) and the case becomes ``FAILED: timeout`` - or ``budget_exhausted``
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
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from meshforge.agent.benchmarks import scores
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
    PCI_BUS_ID,
    Case,
    GpuRef,
    Targets,
    names_match,
    plan_cases,
)
from meshforge.agent.hardware.optional import psutil_module
from meshforge.agent.hardware.probe import sanitize_reason
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
    "measure_conditions",
    "thread_env",
]

log = get_logger(__name__)

CHILD_MODULE: Final = "meshforge.agent.benchmarks.child"
NICE: Final = 10
"""POSIX niceness of the child (D32b); Windows gets ``BELOW_NORMAL_PRIORITY_CLASS``."""
THREAD_VARS: Final = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
LOAD_WINDOW_S: Final = 2.0
"""The window before the first case over which the machine's CPU load is measured."""
BACKGROUND_LOAD_THRESHOLD: Final = 25.0
"""``background_load_high`` when the load before the run exceeds this (percent)."""

_LINE_LIMIT: Final = 1 << 20
_STDERR_TAIL: Final = 2000
_EXIT_GRACE_S: Final = 2.0
_REAP_S: Final = 2.0
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


def thread_env(threads: int) -> dict[str, str]:
    """Environment variables that pin a child's BLAS/OpenMP thread count."""
    if threads < 1:
        raise ValueError("threads must be >= 1")
    return dict.fromkeys(THREAD_VARS, str(threads))


# --------------------------------------------------------------------- child
class ChildError(Exception):
    """A request to the child did not produce an answer.

    ``fatal`` means the child is gone (killed or crashed) and must be replaced;
    otherwise the child answered ``ok: false`` and ``code`` is its failure type.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        fatal: bool,
        reason: MetricReason | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.fatal = fatal
        self.reason = reason


def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and everything it started, now (synchronous, cancel-safe).

    POSIX: the child is the leader of its own session, so ``killpg`` reaches every
    descendant that stayed in it. Windows: ``taskkill /F /T`` walks the tree (a
    bounded blocking call - the one place this path can block the loop, ~0.1 s).
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill") or os.path.join(
            os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe"
        )
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


class ChildProcess:
    """One benchmark child, driven over asyncio pipes. Owned by one task."""

    def __init__(
        self,
        *,
        threads: int,
        lower_priority: bool,
        python: str,
        extra_env: Mapping[str, str],
        on_spawn: Callable[[int], None],
    ) -> None:
        self.threads = threads
        self._lower_priority = lower_priority
        self._python = python
        self._extra_env = dict(extra_env)
        self._on_spawn = on_spawn
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: list[str] = []
        self.dead = False

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> None:
        env = dict(os.environ)
        env.update(thread_env(self.threads))
        env["CUDA_DEVICE_ORDER"] = PCI_BUS_ID
        env.update(self._extra_env)
        argv = [self._python, "-I", "-u", "-m", CHILD_MODULE]
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if self._lower_priority:
                flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
                argv += ["--nice", str(NICE)]
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
            if self._lower_priority:
                argv += ["--nice", str(NICE)]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_LINE_LIMIT,
                **kwargs,
            )
        except (OSError, ValueError) as exc:
            self.dead = True
            raise ChildError(
                "child_not_started",
                f"cannot start the benchmark child: {type(exc).__name__}",
                fatal=True,
                reason=MetricReason.CHILD_NOT_STARTED,
            ) from exc
        self._on_spawn(self._proc.pid)
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"bench-child-stderr:{self._proc.pid}"
        )

    async def _drain_stderr(self) -> None:
        """Keep the stderr pipe empty (a chatty library must not block the child)."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(OSError, ValueError, asyncio.LimitOverrunError):
            while chunk := await proc.stderr.read(4096):
                self._stderr.append(chunk.decode("utf-8", "replace"))
                text = "".join(self._stderr)
                self._stderr = [text[-_STDERR_TAIL:]]

    @property
    def stderr_tail(self) -> str:
        return sanitize_reason("".join(self._stderr)[-400:])

    async def request(
        self, payload: Mapping[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        """One request/answer. Raises :class:`ChildError`; kills the child if fatal."""
        proc = self._proc
        if proc is None or self.dead or proc.stdin is None or proc.stdout is None:
            raise ChildError(
                "child_gone",
                "the child is not running",
                fatal=True,
                reason=MetricReason.CHILD_CRASHED,
            )
        op = str(payload.get("op"))
        try:
            proc.stdin.write((json.dumps(payload, allow_nan=False) + "\n").encode())
            await asyncio.wait_for(proc.stdin.drain(), max(timeout_s, _MIN_TIMEOUT_S))
            line = await asyncio.wait_for(
                proc.stdout.readline(), max(timeout_s, _MIN_TIMEOUT_S)
            )
        except TimeoutError:
            await self.kill()
            raise ChildError(
                "timeout",
                f"no answer to {op!r} within {timeout_s:.3g} s; the child was killed",
                fatal=True,
                reason=MetricReason.TIMEOUT,
            ) from None
        except (OSError, ValueError, asyncio.LimitOverrunError) as exc:
            await self.kill()
            raise ChildError(
                "child_crashed",
                f"lost the child while sending {op!r}: {type(exc).__name__}",
                fatal=True,
                reason=MetricReason.CHILD_CRASHED,
            ) from exc
        if not line:
            code = await self._exit_code()
            await self.kill()
            raise ChildError(
                "child_crashed",
                f"the child exited (code {code}) during {op!r}: "
                f"{self.stderr_tail or 'no stderr output'}",
                fatal=True,
                reason=MetricReason.CHILD_CRASHED,
            )
        try:
            answer = json.loads(line)
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            await self.kill()
            raise ChildError(
                "bad_answer",
                f"unreadable answer to {op!r}: {sanitize_reason(repr(line[:120]))}",
                fatal=True,
                reason=MetricReason.BAD_ANSWER,
            )
        if not answer.get("ok"):
            raise ChildError(
                str(answer.get("failure_type", "benchmark_error")),
                sanitize_reason(str(answer.get("message", ""))),
                fatal=False,
            )
        return answer

    async def call(
        self, payload: Mapping[str, Any], key: str, timeout_s: float
    ) -> dict[str, Any]:
        """:meth:`request`, then the answer's ``key`` object (``info`` / ``sample``);
        an answer without it is protocol garbage: the child is killed."""
        answer = await self.request(payload, timeout_s)
        value = answer.get(key)
        if not isinstance(value, dict):
            await self.kill()
            raise ChildError(
                "bad_answer",
                f"answer to {payload.get('op')!r} has no {key!r} object",
                fatal=True,
                reason=MetricReason.BAD_ANSWER,
            )
        return value

    async def _exit_code(self) -> int | None:
        proc = self._proc
        if proc is None:
            return None
        with contextlib.suppress(TimeoutError):
            return await asyncio.wait_for(proc.wait(), _REAP_S)
        return proc.returncode

    def kill_now(self) -> None:
        """Synchronous tree kill; safe to call from a cancellation path."""
        if self._proc is not None:
            self.dead = True
            kill_tree(self._proc)

    async def kill(self) -> None:
        self.kill_now()
        await self.reap()

    async def close(self) -> None:
        """Ask the child to exit (``bye``), kill it if it does not, reap it."""
        proc = self._proc
        if proc is None:
            return
        if not self.dead and proc.returncode is None and proc.stdin is not None:
            with contextlib.suppress(OSError, ValueError, TimeoutError):
                proc.stdin.write(b'{"op": "bye"}\n')
                await asyncio.wait_for(proc.stdin.drain(), _EXIT_GRACE_S)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), _EXIT_GRACE_S)
        self.kill_now()
        await self.reap()

    async def reap(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), _REAP_S)
        task = self._stderr_task
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), _REAP_S)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


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
