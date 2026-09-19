"""Orchestration of one S7 run: capabilities -> cases -> repetitions -> envelope.

The case list is built from what the machine can actually do
(:func:`plan_cases`), so every laptop runs the same *rules* even though it may
run different backends:

======================  ==================================================
case                    produced when
======================  ==================================================
``cpu-<dtype>-<t>``     torch or numpy is importable (real GFLOPS)
``cpu-python-<t>``      always (comparable interpreter-bound baseline)
``memory-bandwidth``    always (all four kernels with torch/numpy, else copy)
``gpu-<dtype>``         torch reports a working CUDA device
``gpu-copy``            torch reports a working CUDA device
======================  ==================================================

Anything that cannot run is recorded as a typed run-level error with the reason
(``torch_not_installed``, ``cuda_unavailable``, ``budget_exhausted``, ...) - the
result file never just omits a metric.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from _inventory import probes
from spikes._harness.envelope import make_envelope
from spikes._harness.reps import (
    JSON,
    CaseResult,
    RepError,
    Sample,
    error_record,
    run_reps,
)
from spikes._s7 import SPIKE, TOPIC, environment, scores, sizing
from spikes._s7.sizing import Profile
from spikes._s7.worker import WorkerError, WorkerGroup, probe_capabilities

#: Units of every metric S7 can produce (the report groups by metric name).
METRIC_UNITS: Final[dict[str, str]] = {
    "gflops": "GFLOP/s",
    "python_mflops": "MFLOP/s",
    "tflops": "TFLOP/s",
    "event_tflops": "TFLOP/s",
    "copy_gb_s": "GB/s",
    "scale_gb_s": "GB/s",
    "add_gb_s": "GB/s",
    "triad_gb_s": "GB/s",
    "device_copy_gb_s": "GB/s",
    "elapsed_s": "s",
}
#: What the planner (ARCHITECTURE §7) reads out of an S7 result.
PLANNER_FIELDS: Final[dict[str, str]] = {
    "cpu-float32-all.summary.gflops.mean": (
        "node CPU throughput for T_compute of CPU workloads (all cores)"
    ),
    "cpu-float64-all.summary.gflops.mean": "fp64 CPU throughput (scientific workloads)",
    "cpu-float32-1t.summary.gflops.mean": "single-core throughput for serial phases",
    "memory-bandwidth.summary.triad_gb_s.mean": (
        "host memory bandwidth: the ceiling for data-heavy CPU tasks and for "
        "staging tensors before a transfer"
    ),
    "gpu-float32.summary.tflops.mean": "rate_g for fp32 GPU work (Modes 2 and 4)",
    "gpu-float16.summary.tflops.mean": "rate_g for fp16/mixed-precision inference",
    "gpu-copy.summary.device_copy_gb_s.mean": (
        "device memory bandwidth: upper bound of on-GPU data movement"
    ),
    "extra.stability.*.thermal_throttling_suspected": (
        "confidence flag: a throttled node's score is optimistic"
    ),
    "extra.on_battery": "scores measured on battery are not comparable; ignore them",
}
_MAX_FALLBACK_WORKERS: Final = 16


class Budget:
    """The whole-run wall-clock budget (``--timeout``), like inventory's."""

    def __init__(self, total_s: float, clock: Callable[[], float] = time.monotonic):
        self.total_s = total_s
        self._clock = clock
        self._deadline = clock() + total_s

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def slice_for(self, wanted_s: float) -> float:
        return max(0.0, min(wanted_s, self.remaining()))


@dataclass(frozen=True, slots=True)
class CasePlan:
    """One measurement case: what to run, where, and how to label it."""

    case_id: str
    label: str
    spec: dict[str, JSON]
    metrics: dict[str, str]
    workers: int = 1
    threads: int = 1
    attrs: dict[str, JSON] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Everything the operator chose for this run."""

    profile: Profile
    #: S7 sends no network traffic: ``None`` = "not applicable" in the envelope
    link: str | None = None
    name: str | None = None
    reps: int = 0
    warmup: int = -1
    budget_s: float = 0.0
    skip_gpu: bool = False
    device_index: int = 0
    pause_s: float = 0.2

    def effective_reps(self) -> int:
        return self.reps or self.profile.reps

    def effective_warmup(self) -> int:
        return self.profile.warmup if self.warmup < 0 else self.warmup

    def effective_budget(self) -> float:
        return self.budget_s or self.profile.budget_s


@dataclass(slots=True)
class HardwareRun:
    """Result of one machine's run."""

    envelope: dict[str, JSON]
    complete: bool
    warnings: list[str]


def metric_units(names: Mapping[str, JSON] | list[str]) -> dict[str, str]:
    """Units for a set of metric names (unknown names get an empty unit)."""
    return {str(name): METRIC_UNITS.get(str(name), "") for name in names}


def plan_cases(
    caps: Mapping[str, JSON],
    profile: Profile,
    *,
    cpu_count: int,
    memory_bytes: int,
    skip_gpu: bool = False,
    device_index: int = 0,
) -> tuple[list[CasePlan], list[dict[str, JSON]]]:
    """Build the case list for this machine plus typed errors for what it cannot do."""
    plans: list[CasePlan] = []
    errors: list[dict[str, JSON]] = []
    all_threads = max(1, cpu_count)
    blas = "torch" if caps.get("torch") else ("numpy" if caps.get("numpy") else None)
    if blas is None:
        errors.append(
            {
                "failure_type": "no_blas_backend",
                "message": (
                    "neither torch nor numpy is installed: no real GFLOPS number "
                    "on this machine, only the interpreter-bound python fallback "
                    "(install torch, ADR-0011)"
                ),
            }
        )
    else:
        for dtype in ("float32", "float64"):
            for threads, suffix in ((1, "1t"), (all_threads, "all")):
                plans.append(
                    CasePlan(
                        case_id=f"cpu-{dtype}-{suffix}",
                        label=f"CPU {dtype} matmul, {threads} thread(s), {blas}",
                        spec={
                            "kind": "cpu_matmul",
                            "dtype": dtype,
                            "threads": threads,
                            "target_s": profile.cpu_target_s,
                        },
                        metrics=metric_units(["gflops", "elapsed_s"]),
                        threads=threads,
                        attrs={
                            "backend_family": blas,
                            "dtype": dtype,
                            "threads": threads,
                        },
                    )
                )
    for workers, suffix in (
        (1, "1t"),
        (min(all_threads, _MAX_FALLBACK_WORKERS), "all"),
    ):
        plans.append(
            CasePlan(
                case_id=f"cpu-python-{suffix}",
                label=f"CPU SAXPY, pure Python fallback, {workers} process(es)",
                spec={"kind": "cpu_python", "target_s": profile.cpu_target_s},
                metrics=metric_units(["python_mflops", "elapsed_s"]),
                workers=workers,
                attrs={"backend_family": "python_fallback", "processes": workers},
            )
        )
    plans.append(
        CasePlan(
            case_id="memory-bandwidth",
            label="Host memory bandwidth (STREAM copy/scale/add/triad)",
            spec={"kind": "mem_bw", "bytes": memory_bytes},
            # The exact op list is known only after setup (backend dependent).
            metrics=metric_units(["copy_gb_s", "elapsed_s"]),
            threads=1,
            attrs={"bytes_requested": memory_bytes},
        )
    )
    plans += _gpu_plans(
        caps, profile, skip_gpu=skip_gpu, index=device_index, errors=errors
    )
    return plans, errors


def _gpu_plans(
    caps: Mapping[str, JSON],
    profile: Profile,
    *,
    skip_gpu: bool,
    index: int,
    errors: list[dict[str, JSON]],
) -> list[CasePlan]:
    cuda = dict(caps.get("cuda") or {})
    if skip_gpu:
        errors.append(
            {"failure_type": "gpu_skipped", "message": "--skip-gpu was given"}
        )
        return []
    if not cuda.get("available"):
        errors.append(
            {
                "failure_type": str(cuda.get("failure_type", "cuda_unavailable")),
                "message": str(cuda.get("error", "no CUDA device reported by torch")),
            }
        )
        return []
    dtypes = ["float32", "float16"]
    if cuda.get("bf16_supported"):
        dtypes.append("bfloat16")
    else:
        errors.append(
            {
                "failure_type": "unsupported_dtype",
                "message": "this GPU does not support bfloat16; skipped",
            }
        )
    plans = [
        CasePlan(
            case_id=f"gpu-{dtype}",
            label=f"GPU {dtype} matmul",
            spec={
                "kind": "gpu_matmul",
                "dtype": dtype,
                "device_index": index,
                "target_s": profile.gpu_target_s,
            },
            metrics=metric_units(["tflops", "event_tflops", "elapsed_s"]),
            attrs={"backend_family": "torch-cuda", "dtype": dtype},
        )
        for dtype in dtypes
    ]
    plans.append(
        CasePlan(
            case_id="gpu-copy",
            label="GPU device-to-device copy bandwidth",
            spec={
                "kind": "gpu_copy",
                "device_index": index,
                "target_s": profile.gpu_target_s,
                "bytes": sizing.DEFAULT_MEMORY_BYTES,
            },
            metrics=metric_units(["device_copy_gb_s", "elapsed_s"]),
            attrs={"backend_family": "torch-cuda"},
        )
    )
    return plans


def _failed_case(plan: CasePlan, cfg: RunConfig, exc: RepError) -> CaseResult:
    result = CaseResult(
        case_id=plan.case_id,
        label=plan.label,
        metrics=dict(plan.metrics),
        attrs=dict(plan.attrs),
        requested_reps=cfg.effective_reps(),
        requested_warmup=cfg.effective_warmup(),
        aborted=True,
    )
    result.errors.append(error_record(0, "setup", exc))
    return result


def execute_case(
    plan: CasePlan,
    cfg: RunConfig,
    budget: Budget,
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> CaseResult:
    """Run one case in its own child process(es). Never raises on measurement errors."""
    profile = cfg.profile
    group = WorkerGroup(plan.workers, threads=plan.threads)
    try:
        with group:
            try:
                infos = group.setup(
                    plan.spec, budget.slice_for(profile.setup_timeout_s)
                )
            except WorkerError as exc:
                log(f"{plan.case_id}: setup failed: {exc.failure_type}: {exc}")
                return _failed_case(
                    plan, cfg, RepError(exc.failure_type, str(exc), fatal=True)
                )
            metrics = dict(plan.metrics)
            ops = infos[0].get("ops") if infos else None
            if isinstance(ops, list):
                metrics = metric_units([f"{op}_gb_s" for op in ops] + ["elapsed_s"])

            def measure(_index: int) -> Sample:
                if budget.expired():
                    raise RepError(
                        "budget_exhausted",
                        f"the run budget of {budget.total_s:g} s ran out",
                        fatal=True,
                    )
                try:
                    return group.rep(budget.slice_for(profile.rep_timeout_s))
                except WorkerError as exc:
                    raise RepError(exc.failure_type, str(exc), fatal=exc.fatal) from exc

            attrs: dict[str, JSON] = {
                **plan.attrs,
                "setup": infos[0] if len(infos) == 1 else infos,
                "workers": plan.workers,
                "threads_requested": plan.threads,
            }
            result = run_reps(
                plan.case_id,
                measure,
                reps=cfg.effective_reps(),
                warmup=cfg.effective_warmup(),
                metrics=metrics,
                label=plan.label,
                attrs=attrs,
                pause_s=cfg.pause_s,
            )
    finally:
        group.close()
    elapsed = [
        float(s["elapsed_s"])
        for s in result.samples
        if isinstance(s.get("elapsed_s"), int | float)
    ]
    result.attrs["stability"] = scores.stability(elapsed)
    return result


def machine_sizes(profile: Profile) -> dict[str, JSON]:
    """Memory-bandwidth buffer size and the facts it was derived from."""
    memory = probes.probe_memory()
    available = None
    if memory.get("ok"):
        value = memory.get("value") or {}
        available = value.get("available_bytes") or value.get("total_bytes")
    cache = environment.last_level_cache_bytes()
    return {
        "last_level_cache_bytes": cache,
        "available_ram_bytes": available,
        "memory_buffer_bytes": sizing.memory_buffer_bytes(
            cache_bytes=cache,
            available_bytes=int(available) if available else None,
            profile_bytes=profile.memory_bytes,
            quick=profile.quick,
        ),
    }


def run_scores(
    cfg: RunConfig, *, log: Callable[[str], None] = lambda _m: None
) -> HardwareRun:
    """Measure this machine and return the result envelope."""
    profile = cfg.profile
    budget = Budget(cfg.effective_budget())
    warnings: list[str] = []
    if profile.quick:
        warnings.append(
            "QUICK RUN: sizes and warmup are reduced; these numbers are a smoke "
            "test, not a measurement of this machine"
        )
    before = environment.snapshot("before")
    if before["on_battery"]:
        warnings.append(
            "ON BATTERY: CPU and GPU clocks are cut hard on battery power, so "
            "these scores are NOT comparable with the other laptops. Plug the "
            "machine in and run again."
        )
    elif before["on_battery"] is None:
        warnings.append("power source unknown: cannot confirm the machine is on AC")
    log("probing backends (torch / numpy / CUDA) in a child process ...")
    caps = probe_capabilities(budget.slice_for(profile.setup_timeout_s))
    sizes = machine_sizes(profile)
    plans, errors = plan_cases(
        caps,
        profile,
        cpu_count=int(caps.get("cpu_count") or 1),
        memory_bytes=int(sizes["memory_buffer_bytes"]),
        skip_gpu=cfg.skip_gpu,
        device_index=cfg.device_index,
    )
    warnings += [f"{e['failure_type']}: {e['message']}" for e in errors]
    cases: list[CaseResult] = []
    for plan in plans:
        if budget.expired():
            errors.append(
                {
                    "failure_type": "budget_exhausted",
                    "message": f"{plan.case_id} was not started: the "
                    f"{budget.total_s:g} s run budget ran out",
                }
            )
            continue
        log(f"case {plan.case_id}: {plan.label} ...")
        cases.append(execute_case(plan, cfg, budget, log=log))
    after = environment.snapshot("after")
    stability = {c.case_id: c.attrs.get("stability") for c in cases}
    throttled = sorted(
        case_id
        for case_id, value in stability.items()
        if isinstance(value, dict) and value.get("thermal_throttling_suspected")
    )
    if throttled:
        warnings.append(
            "thermal throttling suspected (last repetition much slower than the "
            f"first): {', '.join(throttled)}"
        )
    complete = not errors and all(
        not c.aborted and len(c.samples) >= cfg.effective_reps() for c in cases
    )
    envelope = make_envelope(
        spike=SPIKE,
        topic=TOPIC,
        link=cfg.link,
        host_label=cfg.name,
        params={
            "profile": profile.name,
            "quick": profile.quick,
            "reps": cfg.effective_reps(),
            "warmup": cfg.effective_warmup(),
            "budget_s": cfg.effective_budget(),
            "pause_s": cfg.pause_s,
            "skip_gpu": cfg.skip_gpu,
            "device_index": cfg.device_index,
            "sizes": sizes,
        },
        cases=cases,
        errors=errors,
        extra={
            "quick": profile.quick,
            "capabilities": caps,
            "gpu": caps.get("cuda"),
            "environment": {"before": before, "after": after},
            "on_battery": before["on_battery"] or after["on_battery"],
            "stability": stability,
            "warnings": warnings,
            "budget_remaining_s": round(budget.remaining(), 3),
            "planner_fields": PLANNER_FIELDS,
            "metric_units": METRIC_UNITS,
        },
        tool={"script": "lab/spikes/s7_hardware.py", "spike": SPIKE},
    )
    return HardwareRun(envelope=envelope, complete=complete, warnings=warnings)
