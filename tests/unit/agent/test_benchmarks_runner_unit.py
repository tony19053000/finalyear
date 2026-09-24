"""G7-A (0.3.4): the runner's pure helpers - failure mapping, sample checks, the
second device check, ordering. No child process (those are component tests)."""

from __future__ import annotations

import math
from collections import namedtuple
from typing import Any

import pytest

from meshforge.agent.benchmarks import runner as r
from meshforge.agent.benchmarks.model import (
    GPU_METRICS,
    HOST_METRICS,
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
)
from meshforge.agent.benchmarks.plan import Case, GpuRef

pytestmark = pytest.mark.unit

GPU = "NVIDIA GeForce RTX 3050 Laptop GPU"
CPU_CASE = Case(MetricName.CPU_FP32_GFLOPS_ALL, {"kind": "cpu_matmul"}, threads=8)
GPU_CASE = Case(
    MetricName.GPU_FP32_TFLOPS, {"kind": "gpu_matmul"}, threads=1, gpu_index=0
)


def _runner(clock: list[float] | None = None) -> r.ScoreRunner:
    now = clock or [0.0]
    runner = r.ScoreRunner(
        r.RunnerConfig(budget_s=10, case_timeout_s=5),
        r.HostFacts(cpu_threads=8, gpus=(GpuRef(0, GPU),)),
        fingerprint="a" * 64,
        clock=lambda: now[0],
    )
    runner._deadline = 10.0
    return runner


@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        ("no_blas_backend", MetricStatus.UNAVAILABLE, MetricReason.NO_BLAS_BACKEND),
        (
            "torch_not_installed",
            MetricStatus.UNAVAILABLE,
            MetricReason.TORCH_NOT_INSTALLED,
        ),
        ("cuda_broken", MetricStatus.UNAVAILABLE, MetricReason.CUDA_UNAVAILABLE),
        ("unsupported_dtype", MetricStatus.UNAVAILABLE, MetricReason.UNSUPPORTED_DTYPE),
        ("device_mismatch", MetricStatus.FAILED, MetricReason.DEVICE_MISMATCH),
        ("insufficient_vram", MetricStatus.FAILED, MetricReason.INSUFFICIENT_VRAM),
        ("gpu_rep_failed", MetricStatus.FAILED, MetricReason.GPU_REP_FAILED),
        ("something_new", MetricStatus.FAILED, MetricReason.BENCHMARK_ERROR),
    ],
)
def test_child_failure_codes_map_to_typed_statuses(
    code: str, status: MetricStatus, reason: MetricReason
) -> None:
    metric = _runner()._failure(GPU_CASE, r.ChildError(code, "msg", fatal=False))
    assert (metric.status, metric.reason) == (status, reason)
    if reason is MetricReason.BENCHMARK_ERROR:
        assert code in metric.detail  # the unknown code is kept for the operator


def test_a_timeout_after_the_budget_ran_out_is_budget_exhausted() -> None:
    clock = [3.0]
    runner = _runner(clock)
    err = r.ChildError("timeout", "slow", fatal=True, reason=MetricReason.TIMEOUT)
    assert runner._failure(CPU_CASE, err).reason is MetricReason.TIMEOUT
    clock[0] = 10.5
    assert runner._failure(CPU_CASE, err).reason is MetricReason.BUDGET_EXHAUSTED


def test_the_device_a_gpu_case_opened_must_be_nvmls_device() -> None:
    runner = _runner()
    assert runner._setup_mismatch(GPU_CASE, {"device_name": GPU}) is None
    assert runner._setup_mismatch(GPU_CASE, {"device_name": "NVIDIA RTX A2000"})
    assert runner._setup_mismatch(GPU_CASE, {})
    assert runner._setup_mismatch(CPU_CASE, {}) is None
    other = Case(MetricName.GPU_MEM_GB_S, {}, threads=1, gpu_index=5)
    assert runner._setup_mismatch(other, {"device_name": GPU})  # no such NVML GPU


def _samples(values: list[Any]) -> list[dict[str, Any]]:
    return [{"value": v, "elapsed_s": 0.1} for v in values]


def test_summarise_computes_mean_stdev_and_flags() -> None:
    info = {
        "backend": "numpy",
        "backend_version": "2.5.3",
        "threads_effective": 8,
        "lower_bound": True,
    }
    metric = r._summarise(CPU_CASE, info, _samples([10.0, 12.0, 11.0, 9.0, 8.0]))
    assert metric.status is MetricStatus.OK
    assert metric.mean == pytest.approx(10.0)
    assert metric.stdev == pytest.approx(math.sqrt(2.5))
    assert metric.n == 5 and metric.lower_bound and metric.threads_effective == 8
    assert metric.throttling_suspected is False


@pytest.mark.parametrize("bad", [math.nan, math.inf, 0.0, -1.0, None, "12", True])
def test_summarise_rejects_a_bad_sample_instead_of_storing_it(bad: Any) -> None:
    metric = r._summarise(CPU_CASE, {}, _samples([10.0, 10.0, bad, 10.0, 10.0]))
    assert metric.status is MetricStatus.FAILED
    assert metric.reason is MetricReason.BAD_SAMPLE
    assert metric.mean is None


def test_throttling_is_flagged_when_the_last_rep_is_much_slower() -> None:
    samples = [{"value": 10.0, "elapsed_s": t} for t in (0.1, 0.1, 0.1, 0.1, 0.2)]
    assert r._summarise(CPU_CASE, {}, samples).throttling_suspected is True
    short = [{"value": 10.0, "elapsed_s": t} for t in (0.01, 0.01, 0.01, 0.01, 0.02)]
    assert (
        r._summarise(CPU_CASE, {}, short).throttling_suspected is None
    )  # inconclusive


def test_metrics_come_out_in_canonical_order() -> None:
    gpus = (GpuRef(1, "b"), GpuRef(0, "a"))
    shuffled: list[Metric] = [
        Metric.not_measured(
            n, MetricStatus.FAILED, MetricReason.TIMEOUT, gpu_index=g.index
        )
        for g in gpus
        for n in reversed(GPU_METRICS)
    ] + [
        Metric.not_measured(n, MetricStatus.FAILED, MetricReason.TIMEOUT)
        for n in reversed(HOST_METRICS)
    ]
    keys = [m.key for m in r._ordered(shuffled, gpus)]
    assert keys[:4] == [n.value for n in HOST_METRICS]
    assert keys[4:7] == [f"gpu0.{n.value}" for n in GPU_METRICS]
    assert keys[7:] == [f"gpu1.{n.value}" for n in GPU_METRICS]


def test_thread_env_pins_every_blas_variable() -> None:
    env = r.thread_env(6)
    assert set(env) == set(r.THREAD_VARS) and set(env.values()) == {"6"}
    with pytest.raises(ValueError):
        r.thread_env(0)


def test_busy_percent_from_cpu_times() -> None:
    T = namedtuple("T", "user system idle iowait")
    assert r.busy_percent(T(0, 0, 0, 0), T(30, 10, 55, 5)) == pytest.approx(40.0)
    assert r.busy_percent(T(1, 1, 1, 1), T(1, 1, 1, 1)) is None
