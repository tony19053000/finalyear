"""Shared fakes for the G7 hardware-score tests (imported as ``mf_bench_fakes``).

No torch, no numpy: :func:`block_backends` makes both imports fail
(``sys.modules[name] = None``, the pattern SESSION_HANDOFF §2 prescribes), and
:class:`FakeTorch` is S7's fake-torch pattern cut down to what the agent kernels
call. Builders for ``HardwareFacts`` and ``ScoreSet`` keep the tests short.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from meshforge.agent.benchmarks.model import (
    Conditions,
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
    ScoreSet,
)
from meshforge.agent.hardware.cpu import CpuFacts
from meshforge.agent.hardware.gpu import GpuFacts
from meshforge.agent.hardware.summary import HardwareFacts
from meshforge.common.ids import new_score_run_id

__all__ = [
    "FakeTorch",
    "block_backends",
    "make_facts",
    "make_gpu",
    "make_scoreset",
    "ok_metric",
]

GPU_NAME = "NVIDIA GeForce RTX 3050 Laptop GPU"


def block_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import torch`` / ``import numpy`` fail and clear the kernel cache."""
    from meshforge.agent.benchmarks import kernels

    for name in ("torch", "numpy"):
        monkeypatch.setitem(sys.modules, name, None)
    kernels._module.cache_clear()


def make_gpu(index: int = 0, model: str = GPU_NAME, **kw: Any) -> GpuFacts:
    base: dict[str, Any] = {
        "index": index,
        "model": model,
        "vram_bytes": 4 * 2**30,
        "driver_version": "595.91.07",
        "compute_capability": "8.6",
        "source": "nvml",
    }
    base.update(kw)
    return GpuFacts(**base)


def make_facts(
    *,
    model: str = "Fictional CPU 9000",
    threads: int = 8,
    ram_bytes: int = 16 * 2**30,
    gpus: tuple[GpuFacts, ...] = (),
) -> HardwareFacts:
    return HardwareFacts(
        cpu=CpuFacts(
            model=model,
            physical_cores=max(1, threads // 2),
            logical_threads=threads,
            ram_bytes=ram_bytes,
            source="test",
        ),
        gpus=gpus,
    )


def ok_metric(
    name: MetricName, mean: float = 100.0, gpu_index: int | None = None
) -> Metric:
    return Metric(
        name=name,
        gpu_index=gpu_index,
        status=MetricStatus.OK,
        mean=mean,
        stdev=mean / 100,
        n=5,
        backend="numpy",
        backend_version="2.5.3",
        threads_effective=8,
        throttling_suspected=False,
    )


def make_scoreset(fingerprint: str = "a" * 64, **kw: Any) -> ScoreSet:
    base: dict[str, Any] = {
        "run_id": new_score_run_id(),
        "measured_at": datetime(2026, 9, 24, 12, 0, 0, 123456, tzinfo=UTC),
        "score_version": 1,
        "fingerprint": fingerprint,
        "conditions": Conditions(
            on_battery=False,
            cpu_load_percent_before=3.5,
            background_load_high=False,
            priority_lowered=True,
            budget_s=90.0,
            duration_s=31.25,
        ),
        "metrics": (
            ok_metric(MetricName.CPU_FP32_GFLOPS_1T, 97.31354540848592),
            ok_metric(MetricName.MEM_TRIAD_GB_S, 19.290167145880428),
            Metric.not_measured(
                MetricName.GPU_FP32_TFLOPS,
                MetricStatus.UNAVAILABLE,
                MetricReason.TORCH_NOT_INSTALLED,
                "torch is not installed",
                gpu_index=0,
            ),
        ),
    }
    base.update(kw)
    return ScoreSet(**base)


# ------------------------------------------------------------------ fake torch
class _FakeTensor:
    def __init__(self, n: int, dtype: str) -> None:
        self.n = n
        self.dtype = dtype

    def copy_(self, _other: _FakeTensor) -> _FakeTensor:
        return self


class _FakeCuda:
    def __init__(self, names: list[str], free: int, available: bool) -> None:
        self.names = names
        self.free = free
        self.available = available

    def is_available(self) -> bool:
        return self.available

    def device_count(self) -> int:
        return len(self.names)

    def mem_get_info(self, _index: int = 0) -> tuple[int, int]:
        return self.free, 4 * 2**30

    def get_device_name(self, index: int) -> str:
        return self.names[index]

    def synchronize(self, _device: object = None) -> None:
        return None


class _Flag:
    allow_tf32 = True


class FakeTorch:
    """Enough torch for the agent kernels' GPU paths and ``capabilities()``."""

    __version__ = "2.14.0+cu130"
    float32 = "fp32"
    float16 = "fp16"
    float64 = "fp64"

    def __init__(
        self,
        names: list[str] | None = None,
        *,
        free: int = 3 * 2**30,
        available: bool = True,
    ) -> None:
        self.cuda = _FakeCuda([GPU_NAME] if names is None else names, free, available)
        self.version = type("V", (), {"cuda": "13.0"})()
        self.backends = type(
            "B", (), {"cuda": type("C", (), {"matmul": _Flag()})(), "cudnn": _Flag()}
        )()

    def device(self, kind: str, index: int) -> str:
        return f"{kind}:{index}"

    def rand(self, shape: tuple[int, int], dtype: str, device: str = "") -> _FakeTensor:
        return _FakeTensor(shape[0], dtype)

    def empty(
        self, shape: tuple[int, int], dtype: str, device: str = ""
    ) -> _FakeTensor:
        return _FakeTensor(shape[0], dtype)

    def zeros(self, n: int, dtype: str, device: str = "") -> _FakeTensor:
        return _FakeTensor(n, dtype)

    def mm(self, _a: _FakeTensor, _b: _FakeTensor, out: _FakeTensor) -> _FakeTensor:
        return out
