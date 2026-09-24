"""Which cases to run on this machine, and which metrics are settled without running.

Pure functions (brief §5g D30-D32): the inputs are the benchmark child's ``--probe``
capabilities, the GPUs discovery listed (``HardwareFacts.gpus`` - the NVML view
that ``Hello.hardware`` carries), the thread count and the RAM available now. The
output is a :class:`Plan`: the cases for :mod:`.runner`, plus every metric that is
already decided (``UNAVAILABLE`` / ``FAILED``) with its typed reason. Every metric of
D31 appears exactly once across the two - nothing is silently omitted.

Rules:

* **CPU / memory** - torch if importable, else numpy (D30); neither ->
  ``UNAVAILABLE: no_blas_backend`` for all four host metrics. The pure-Python
  fallback of S7 is not a score.
* **GPU, per GPU in** ``Hello.hardware`` - no torch -> ``UNAVAILABLE:
  torch_not_installed``; torch but no usable CUDA -> ``UNAVAILABLE: cuda_unavailable``
  (the child's own code is kept in ``detail``); and the **device-mismatch rule**: the
  device torch calls index *i* must be the device NVML calls index *i* (same name,
  and the child ran with ``CUDA_DEVICE_ORDER=PCI_BUS_ID``), otherwise the three GPU
  metrics of that GPU are ``FAILED: device_mismatch`` - a score attached to the
  wrong card is exactly the silent error D32 forbids.
* A probe that itself failed (timeout, crash) fails every metric with its reason.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.agent.benchmarks import sizing
from meshforge.agent.benchmarks.model import (
    GPU_METRICS,
    HOST_METRICS,
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
)

__all__ = [
    "CPU_RAM_FRACTION",
    "GPU_COPY_BYTES",
    "GPU_MAX_N",
    "PCI_BUS_ID",
    "Case",
    "GpuRef",
    "Plan",
    "Targets",
    "names_match",
    "plan_cases",
]

PCI_BUS_ID: Final = "PCI_BUS_ID"
#: Share of available RAM the three CPU matmul matrices may use.
CPU_RAM_FRACTION: Final = 0.10
#: GPU matmul size cap: large enough to load a laptop GPU, small enough for the budget.
GPU_MAX_N: Final = 4096
GPU_COPY_BYTES: Final = 256 * 2**20


@dataclass(frozen=True, slots=True)
class Targets:
    """Per-repetition calibration targets and repetition counts."""

    cpu_target_s: float = 0.2
    mem_target_s: float = 0.1
    gpu_target_s: float = 0.1
    reps: int = 5
    warmup: int = 1

    def __post_init__(self) -> None:
        if self.reps < 5:
            raise ValueError("reps must be >= 5 (an OK metric needs n >= 5)")
        if self.warmup < 0:
            raise ValueError("warmup must be >= 0")
        for value in (self.cpu_target_s, self.mem_target_s, self.gpu_target_s):
            if not (math.isfinite(value) and value > 0):
                raise ValueError("targets must be finite and > 0")


@dataclass(frozen=True, slots=True)
class GpuRef:
    """One GPU as discovery (NVML) sees it."""

    index: int
    model: str


@dataclass(frozen=True, slots=True)
class Case:
    """One measurement: run ``spec`` in a child with ``threads`` BLAS threads."""

    metric: MetricName
    spec: dict[str, Any]
    threads: int
    gpu_index: int | None = None

    @property
    def key(self) -> str:
        if self.gpu_index is None:
            return self.metric.value
        return f"gpu{self.gpu_index}.{self.metric.value}"


@dataclass(frozen=True, slots=True)
class Plan:
    cases: tuple[Case, ...]
    resolved: tuple[Metric, ...] = field(default=())
    backend: str | None = None


def names_match(nvml_model: str, torch_name: str) -> bool:
    """Same device name, ignoring case and whitespace runs; an empty name never matches."""
    a = " ".join(nvml_model.split()).casefold()
    b = " ".join(torch_name.split()).casefold()
    return bool(a) and a == b


def _probe_failure(caps: Mapping[str, Any]) -> tuple[MetricReason, str] | None:
    failure = caps.get("probe_failure")
    if not isinstance(failure, Mapping):
        return None
    try:
        reason = MetricReason(str(failure.get("reason")))
    except ValueError:
        reason = MetricReason.BENCHMARK_ERROR
    return reason, f"capability probe failed: {failure.get('detail', '')}"


def plan_cases(
    caps: Mapping[str, Any],
    *,
    gpus: Sequence[GpuRef],
    cpu_threads: int,
    available_ram_bytes: int | None,
    targets: Targets | None = None,
) -> Plan:
    """The plan for this machine; see the module docstring for the rules."""
    targets = targets or Targets()
    all_threads = max(1, cpu_threads)
    failed = _probe_failure(caps)
    if failed is not None:
        reason, detail = failed
        failures = [
            Metric.not_measured(name, MetricStatus.FAILED, reason, detail)
            for name in HOST_METRICS
        ]
        failures += [
            Metric.not_measured(
                name, MetricStatus.FAILED, reason, detail, gpu_index=gpu.index
            )
            for gpu in gpus
            for name in GPU_METRICS
        ]
        return Plan(cases=(), resolved=tuple(failures))

    blas = "torch" if caps.get("torch") else ("numpy" if caps.get("numpy") else None)
    cases: list[Case] = []
    resolved: list[Metric] = []
    if blas is None:
        resolved += [
            Metric.not_measured(
                name,
                MetricStatus.UNAVAILABLE,
                MetricReason.NO_BLAS_BACKEND,
                "neither torch nor numpy is importable (install the agent extra)",
            )
            for name in HOST_METRICS
        ]
    else:
        cases += _host_cases(targets, all_threads, available_ram_bytes)
    for gpu in gpus:
        gpu_cases, gpu_resolved = _gpu_plan(caps, gpu, targets)
        cases += gpu_cases
        resolved += gpu_resolved
    return Plan(cases=tuple(cases), resolved=tuple(resolved), backend=blas)


def _host_cases(
    targets: Targets, all_threads: int, available_ram_bytes: int | None
) -> list[Case]:
    avail = available_ram_bytes or 0

    def cpu(metric: MetricName, dtype: str, threads: int) -> Case:
        itemsize = 8 if dtype == "float64" else 4
        max_n = sizing.max_matmul_n(avail, itemsize, fraction=CPU_RAM_FRACTION)
        return Case(
            metric=metric,
            threads=threads,
            spec={
                "kind": "cpu_matmul",
                "metric": metric.value,
                "dtype": dtype,
                "threads": threads,
                "target_s": targets.cpu_target_s,
                "max_n": max_n,
            },
        )

    return [
        # The 1-thread case first: it shares a child with the probe (same env).
        cpu(MetricName.CPU_FP32_GFLOPS_1T, "float32", 1),
        cpu(MetricName.CPU_FP32_GFLOPS_ALL, "float32", all_threads),
        cpu(MetricName.CPU_FP64_GFLOPS_ALL, "float64", all_threads),
        Case(
            metric=MetricName.MEM_TRIAD_GB_S,
            threads=all_threads,
            spec={
                "kind": "mem_bw",
                "metric": MetricName.MEM_TRIAD_GB_S.value,
                "ops": ["triad"],
                "bytes": sizing.agent_memory_bytes(available_ram_bytes),
                "threads": all_threads,
                "target_s": targets.mem_target_s,
            },
        ),
    ]


def _gpu_plan(
    caps: Mapping[str, Any], gpu: GpuRef, targets: Targets
) -> tuple[list[Case], list[Metric]]:
    def settle(status: MetricStatus, reason: MetricReason, detail: str) -> list[Metric]:
        return [
            Metric.not_measured(name, status, reason, detail, gpu_index=gpu.index)
            for name in GPU_METRICS
        ]

    if not caps.get("torch"):
        return [], settle(
            MetricStatus.UNAVAILABLE,
            MetricReason.TORCH_NOT_INSTALLED,
            "torch is not installed (ADR-0011): no GPU score without it",
        )
    cuda = caps.get("cuda")
    cuda = cuda if isinstance(cuda, Mapping) else {}
    if not cuda.get("available"):
        code = str(cuda.get("failure_type", "cuda_unavailable"))
        return [], settle(
            MetricStatus.UNAVAILABLE,
            MetricReason.CUDA_UNAVAILABLE,
            f"{code}: {cuda.get('error', 'torch reports no usable CUDA device')}",
        )
    mismatch = _device_mismatch(caps, cuda, gpu)
    if mismatch is not None:
        return [], settle(MetricStatus.FAILED, MetricReason.DEVICE_MISMATCH, mismatch)
    common = {"device_index": gpu.index, "target_s": targets.gpu_target_s}
    cases = [
        Case(
            metric=metric,
            threads=1,
            gpu_index=gpu.index,
            spec={
                "kind": "gpu_matmul",
                "metric": metric.value,
                "dtype": dtype,
                "max_n": GPU_MAX_N,
                **common,
            },
        )
        for metric, dtype in (
            (MetricName.GPU_FP32_TFLOPS, "float32"),
            (MetricName.GPU_FP16_TFLOPS, "float16"),
        )
    ]
    cases.append(
        Case(
            metric=MetricName.GPU_MEM_GB_S,
            threads=1,
            gpu_index=gpu.index,
            spec={
                "kind": "gpu_copy",
                "metric": MetricName.GPU_MEM_GB_S.value,
                "bytes": GPU_COPY_BYTES,
                **common,
            },
        )
    )
    return cases, []


def _device_mismatch(
    caps: Mapping[str, Any], cuda: Mapping[str, Any], gpu: GpuRef
) -> str | None:
    """Why torch's index ``gpu.index`` may not be NVML's device, or None if it is."""
    if caps.get("cuda_device_order") != PCI_BUS_ID:
        return "the child did not run with CUDA_DEVICE_ORDER=PCI_BUS_ID"
    devices = cuda.get("devices")
    devices = devices if isinstance(devices, list) else []
    torch_device = next(
        (
            d
            for d in devices
            if isinstance(d, Mapping) and d.get("device_index") == gpu.index
        ),
        None,
    )
    if torch_device is None:
        return f"torch sees {len(devices)} CUDA device(s); none has index {gpu.index}"
    torch_name = str(torch_device.get("device_name", ""))
    if not names_match(gpu.model, torch_name):
        return (
            f"NVML index {gpu.index} is {gpu.model or '(unknown)'!r} but torch's "
            f"device {gpu.index} is {torch_name or '(unknown)'!r}"
        )
    return None
