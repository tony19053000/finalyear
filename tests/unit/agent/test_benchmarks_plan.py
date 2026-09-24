"""G7-A (0.3.4, D30-D32): the case planner per capability set, and the
device-mismatch rule. Pure: no child process, no torch, no numpy."""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.agent.benchmarks import sizing
from meshforge.agent.benchmarks.model import (
    GPU_METRICS,
    HOST_METRICS,
    MetricName,
    MetricReason,
    MetricStatus,
)
from meshforge.agent.benchmarks.plan import (
    GpuRef,
    Plan,
    Targets,
    names_match,
    plan_cases,
)

pytestmark = pytest.mark.unit

GPU = "NVIDIA GeForce RTX 3050 Laptop GPU"
GIB = 2**30


def _caps(
    *,
    torch: str | None = None,
    numpy: str | None = None,
    cuda: dict[str, Any] | None = None,
    order: str | None = "PCI_BUS_ID",
) -> dict[str, Any]:
    return {
        "torch": torch,
        "numpy": numpy,
        "cuda_device_order": order,
        "cuda": cuda or {"available": False, "failure_type": "torch_not_installed"},
    }


def _cuda(*names: str) -> dict[str, Any]:
    return {
        "available": True,
        "devices": [
            {"device_index": i, "device_name": name} for i, name in enumerate(names)
        ],
    }


def _plan(caps: dict[str, Any], gpus: tuple[GpuRef, ...] = (), **kw: Any) -> Plan:
    return plan_cases(
        caps,
        gpus=gpus,
        cpu_threads=kw.pop("threads", 8),
        available_ram_bytes=kw.pop("ram", 8 * GIB),
        **kw,
    )


def _keys(plan: Plan) -> list[str]:
    return sorted([c.key for c in plan.cases] + [m.key for m in plan.resolved])


def _every_metric(gpus: tuple[GpuRef, ...]) -> list[str]:
    keys = [m.value for m in HOST_METRICS]
    keys += [f"gpu{g.index}.{m.value}" for g in gpus for m in GPU_METRICS]
    return sorted(keys)


def _resolved(plan: Plan, name: MetricName, gpu: int | None = None) -> Any:
    return next(m for m in plan.resolved if m.name is name and m.gpu_index == gpu)


# -------------------------------------------------------------- host metrics
def test_numpy_only_plans_all_host_cases_and_no_gpu() -> None:
    plan = _plan(_caps(numpy="2.5.3"))
    assert plan.backend == "numpy"
    assert [c.metric for c in plan.cases] == list(HOST_METRICS)
    assert plan.resolved == ()
    one, fp32_all, fp64_all, mem = plan.cases
    assert one.threads == 1 and one.spec["threads"] == 1
    assert fp32_all.threads == fp64_all.threads == mem.threads == 8
    assert fp64_all.spec["dtype"] == "float64"
    assert mem.spec["ops"] == ["triad"]
    assert all(c.spec["metric"] == c.metric.value for c in plan.cases)


def test_torch_is_the_backend_when_both_are_present() -> None:
    plan = _plan(_caps(torch="2.14.0", numpy="2.5.3"))
    assert plan.backend == "torch"
    assert len(plan.cases) == 4


def test_neither_backend_makes_host_metrics_unavailable_no_blas() -> None:
    gpus = (GpuRef(0, GPU),)
    plan = _plan(_caps(), gpus)
    assert plan.cases == ()
    for name in HOST_METRICS:
        metric = _resolved(plan, name)
        assert metric.status is MetricStatus.UNAVAILABLE
        assert metric.reason is MetricReason.NO_BLAS_BACKEND
    assert _keys(plan) == _every_metric(gpus)


# --------------------------------------------------------------------- GPUs
def test_gpus_listed_but_torch_absent_are_unavailable_torch_not_installed() -> None:
    gpus = (GpuRef(0, GPU), GpuRef(1, "Other GPU"))
    plan = _plan(_caps(numpy="2.5.3"), gpus)
    assert all(c.gpu_index is None for c in plan.cases)
    for gpu in gpus:
        for name in GPU_METRICS:
            metric = _resolved(plan, name, gpu.index)
            assert metric.status is MetricStatus.UNAVAILABLE
            assert metric.reason is MetricReason.TORCH_NOT_INSTALLED
    assert _keys(plan) == _every_metric(gpus)


@pytest.mark.parametrize("code", ["cuda_unavailable", "cuda_broken"])
def test_torch_present_but_cuda_unusable_is_unavailable_cuda(code: str) -> None:
    caps = _caps(
        torch="2.14.0",
        numpy="2.5.3",
        cuda={"available": False, "failure_type": code, "error": "driver says no"},
    )
    plan = _plan(caps, (GpuRef(0, GPU),))
    metric = _resolved(plan, MetricName.GPU_FP16_TFLOPS, 0)
    assert metric.status is MetricStatus.UNAVAILABLE
    assert metric.reason is MetricReason.CUDA_UNAVAILABLE
    assert code in metric.detail  # the child's own code survives as context


def test_working_cuda_plans_three_cases_per_gpu_on_the_nvml_index() -> None:
    gpus = (GpuRef(0, GPU), GpuRef(1, "NVIDIA RTX A2000"))
    plan = _plan(_caps(torch="2.14.0", cuda=_cuda(GPU, "NVIDIA RTX A2000")), gpus)
    gpu_cases = [c for c in plan.cases if c.gpu_index is not None]
    assert [(c.gpu_index, c.metric) for c in gpu_cases] == [
        (g.index, m) for g in gpus for m in GPU_METRICS
    ]
    assert all(c.spec["device_index"] == c.gpu_index for c in gpu_cases)
    assert {c.spec["kind"] for c in gpu_cases} == {"gpu_matmul", "gpu_copy"}
    assert _keys(plan) == _every_metric(gpus)


# --------------------------------------------------------- device mismatch
def test_device_mismatch_when_torch_index_names_another_card() -> None:
    """CUDA's default order would put the faster card first: index 0 != NVML 0."""
    gpus = (GpuRef(0, GPU), GpuRef(1, "NVIDIA RTX A2000"))
    plan = _plan(_caps(torch="2.14.0", cuda=_cuda("NVIDIA RTX A2000", GPU)), gpus)
    assert [c for c in plan.cases if c.gpu_index is not None] == []
    for gpu in gpus:
        for name in GPU_METRICS:
            metric = _resolved(plan, name, gpu.index)
            assert metric.status is MetricStatus.FAILED
            assert metric.reason is MetricReason.DEVICE_MISMATCH


def test_device_mismatch_when_the_child_did_not_use_pci_bus_order() -> None:
    plan = _plan(_caps(torch="2.14.0", cuda=_cuda(GPU), order=None), (GpuRef(0, GPU),))
    metric = _resolved(plan, MetricName.GPU_FP32_TFLOPS, 0)
    assert metric.reason is MetricReason.DEVICE_MISMATCH
    assert "PCI_BUS_ID" in metric.detail


def test_device_mismatch_when_torch_does_not_see_the_index() -> None:
    plan = _plan(
        _caps(torch="2.14.0", cuda=_cuda(GPU)), (GpuRef(0, GPU), GpuRef(1, GPU))
    )
    assert any(c.gpu_index == 0 for c in plan.cases)
    metric = _resolved(plan, MetricName.GPU_MEM_GB_S, 1)
    assert metric.reason is MetricReason.DEVICE_MISMATCH


def test_an_unknown_nvml_model_cannot_be_verified_so_it_fails() -> None:
    plan = _plan(_caps(torch="2.14.0", cuda=_cuda(GPU)), (GpuRef(0, ""),))
    assert _resolved(plan, MetricName.GPU_FP32_TFLOPS, 0).reason is (
        MetricReason.DEVICE_MISMATCH
    )


def test_names_match_ignores_case_and_spacing_only() -> None:
    assert names_match(GPU, "nvidia  geforce RTX 3050 laptop gpu ")
    assert not names_match(GPU, "NVIDIA GeForce RTX 3050 Ti Laptop GPU")
    assert not names_match("", "")


# ------------------------------------------------------------ probe failure
def test_a_failed_probe_fails_every_metric_with_its_reason() -> None:
    gpus = (GpuRef(0, GPU),)
    plan = _plan({"probe_failure": {"reason": "timeout", "detail": "slow"}}, gpus)
    assert plan.cases == ()
    assert _keys(plan) == _every_metric(gpus)
    assert {m.reason for m in plan.resolved} == {MetricReason.TIMEOUT}
    assert {m.status for m in plan.resolved} == {MetricStatus.FAILED}


def test_an_unknown_probe_reason_becomes_benchmark_error() -> None:
    plan = _plan({"probe_failure": {"reason": "weird", "detail": ""}})
    assert {m.reason for m in plan.resolved} == {MetricReason.BENCHMARK_ERROR}


# ------------------------------------------------------------------- sizing
@pytest.mark.parametrize("available", [None, 0, 512 * 2**20, 4 * GIB, 64 * GIB])
def test_memory_footprint_stays_within_min_1gib_and_10pct_available(
    available: int | None,
) -> None:
    plan = _plan(_caps(numpy="2.5.3"), ram=available)
    mem = next(c for c in plan.cases if c.metric is MetricName.MEM_TRIAD_GB_S)
    total = mem.spec["bytes"] * sizing.MEMORY_BUFFERS
    cap = GIB if not available else min(GIB, available // 10)
    assert total <= max(cap, sizing.MIN_MEMORY_BYTES * sizing.MEMORY_BUFFERS)
    assert mem.spec["bytes"] >= sizing.MIN_MEMORY_BYTES


def test_cpu_matmul_size_is_capped_by_available_ram() -> None:
    small = _plan(_caps(numpy="2.5.3"), ram=64 * 2**20)
    big = _plan(_caps(numpy="2.5.3"), ram=64 * GIB)
    n_small = small.cases[2].spec["max_n"]  # fp64 all-threads
    assert 3 * n_small**2 * 8 <= 64 * 2**20 * 0.10 or n_small == sizing.MIN_CPU_N
    assert big.cases[2].spec["max_n"] > n_small


def test_targets_refuse_fewer_than_five_reps() -> None:
    with pytest.raises(ValueError, match="reps"):
        Targets(reps=4)
    with pytest.raises(ValueError):
        Targets(cpu_target_s=0.0)
