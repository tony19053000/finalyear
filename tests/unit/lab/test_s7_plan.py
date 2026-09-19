"""Unit tests for the S7 case plan, run config and budget (0.2.11).

``plan_cases`` is the rule that keeps four different laptops comparable: which
cases exist on a machine, and which typed error is recorded for what it cannot
do. No subprocess and no hardware is involved.
"""

from __future__ import annotations

import json

import pytest

from spikes._harness.reps import JSON, RepError
from spikes._s7 import sizing
from spikes._s7.run import (
    METRIC_UNITS,
    PLANNER_FIELDS,
    Budget,
    RunConfig,
    _failed_case,
    execute_case,
    metric_units,
    plan_cases,
)
from spikes._s7.worker import thread_env

CAPS_TORCH_CUDA: dict[str, JSON] = {
    "torch": "2.14.0+cu126",
    "numpy": "2.3.1",
    "cpu_count": 16,
    "cuda": {
        "available": True,
        "bf16_supported": True,
        "devices": [{"device_name": "NVIDIA GeForce RTX 3050 Laptop GPU"}],
    },
}
CAPS_NO_TORCH: dict[str, JSON] = {
    "torch": None,
    "numpy": None,
    "cpu_count": 8,
    "cuda": {
        "available": False,
        "failure_type": "torch_not_installed",
        "error": "torch is not installed (see ADR-0011)",
    },
}


def _plan(
    caps: dict[str, JSON], **kwargs: object
) -> tuple[list[str], list[dict[str, JSON]]]:
    plans, errors = plan_cases(
        caps,
        sizing.QUICK,
        cpu_count=int(caps["cpu_count"]),
        memory_bytes=32 * 2**20,
        **kwargs,  # type: ignore[arg-type]
    )
    return [p.case_id for p in plans], errors


def test_a_full_machine_gets_cpu_memory_and_gpu_cases() -> None:
    ids, errors = _plan(CAPS_TORCH_CUDA)
    assert ids == [
        "cpu-float32-1t",
        "cpu-float32-all",
        "cpu-float64-1t",
        "cpu-float64-all",
        "cpu-python-1t",
        "cpu-python-all",
        "memory-bandwidth",
        "gpu-float32",
        "gpu-float16",
        "gpu-bfloat16",
        "gpu-copy",
    ]
    assert errors == []


def test_a_machine_without_torch_records_typed_errors_and_still_scores() -> None:
    ids, errors = _plan(CAPS_NO_TORCH)
    assert ids == ["cpu-python-1t", "cpu-python-all", "memory-bandwidth"]
    types = [e["failure_type"] for e in errors]
    assert types == ["no_blas_backend", "torch_not_installed"]
    assert "ADR-0011" in str(errors[1]["message"])


def test_numpy_only_machine_uses_numpy_for_the_real_gflops() -> None:
    caps = {**CAPS_NO_TORCH, "numpy": "2.3.1"}
    plans, errors = plan_cases(
        caps, sizing.FULL, cpu_count=4, memory_bytes=2**20, skip_gpu=True
    )
    matmul = [p for p in plans if p.spec["kind"] == "cpu_matmul"]
    assert len(matmul) == 4
    assert all(p.attrs["backend_family"] == "numpy" for p in matmul)
    assert [p.threads for p in matmul] == [1, 4, 1, 4]
    assert [e["failure_type"] for e in errors] == ["gpu_skipped"]


def test_bf16_is_skipped_with_a_reason_on_an_older_card() -> None:
    caps = {
        **CAPS_TORCH_CUDA,
        "cuda": {**CAPS_TORCH_CUDA["cuda"], "bf16_supported": False},
    }
    ids, errors = _plan(caps)
    assert "gpu-bfloat16" not in ids
    assert "gpu-float16" in ids
    assert [e["failure_type"] for e in errors] == ["unsupported_dtype"]


def test_skip_gpu_records_why_the_gpu_is_missing() -> None:
    ids, errors = _plan(CAPS_TORCH_CUDA, skip_gpu=True)
    assert not [i for i in ids if i.startswith("gpu-")]
    assert [e["failure_type"] for e in errors] == ["gpu_skipped"]


def test_broken_cuda_stack_is_reported_with_its_own_failure_type() -> None:
    caps = {
        **CAPS_TORCH_CUDA,
        "cuda": {
            "available": False,
            "failure_type": "cuda_broken",
            "error": "RuntimeError: CUDA driver version is insufficient",
        },
    }
    ids, errors = _plan(caps)
    assert not [i for i in ids if i.startswith("gpu-")]
    assert errors[0]["failure_type"] == "cuda_broken"
    assert "insufficient" in str(errors[0]["message"])


def test_device_index_reaches_the_gpu_specs() -> None:
    plans, _ = plan_cases(
        CAPS_TORCH_CUDA, sizing.FULL, cpu_count=2, memory_bytes=2**20, device_index=1
    )
    gpu = [p for p in plans if p.case_id.startswith("gpu-")]
    assert all(p.spec["device_index"] == 1 for p in gpu)


def test_fallback_worker_count_is_bounded_on_a_big_machine() -> None:
    plans, _ = plan_cases(
        CAPS_NO_TORCH, sizing.FULL, cpu_count=128, memory_bytes=2**20, skip_gpu=True
    )
    fallback = {p.case_id: p for p in plans if p.spec["kind"] == "cpu_python"}
    assert fallback["cpu-python-1t"].workers == 1
    assert fallback["cpu-python-all"].workers == 16


def test_single_cpu_machine_still_gets_one_worker() -> None:
    plans, _ = plan_cases(
        CAPS_NO_TORCH, sizing.QUICK, cpu_count=1, memory_bytes=2**20, skip_gpu=True
    )
    assert all(p.workers >= 1 and p.threads >= 1 for p in plans)


def test_metric_units_are_known_for_every_case() -> None:
    plans, _ = plan_cases(CAPS_TORCH_CUDA, sizing.FULL, cpu_count=8, memory_bytes=2**20)
    for plan in plans:
        for metric, unit in plan.metrics.items():
            assert unit == METRIC_UNITS[metric], (plan.case_id, metric)
    assert metric_units(["nope"]) == {"nope": ""}


def test_planner_fields_are_documented_for_the_real_case_ids() -> None:
    plans, _ = plan_cases(CAPS_TORCH_CUDA, sizing.FULL, cpu_count=8, memory_bytes=2**20)
    ids = {p.case_id for p in plans}
    for field in PLANNER_FIELDS:
        case = field.split(".", 1)[0]
        if not case.startswith("extra"):
            assert case in ids, field


def test_memory_bandwidth_case_asks_for_every_core() -> None:
    """B1: the STREAM number is sold as the machine ceiling, so it must not be
    a single-core measurement. The plan requests all threads (child env and
    ``torch.set_num_threads``) and the calibration target of the profile."""
    for profile in (sizing.QUICK, sizing.FULL):
        plans, _ = plan_cases(
            CAPS_TORCH_CUDA, profile, cpu_count=12, memory_bytes=32 * 2**20
        )
        mem = next(p for p in plans if p.case_id == "memory-bandwidth")
        assert mem.threads == 12
        assert mem.spec["threads"] == 12
        assert mem.spec["target_s"] == profile.mem_target_s
        assert mem.attrs["threads"] == 12
        assert thread_env(mem.threads)["OMP_NUM_THREADS"] == "12"
    assert "all cores" in PLANNER_FIELDS["memory-bandwidth.summary.triad_gb_s.mean"]
    assert "lower bound" in PLANNER_FIELDS["memory-bandwidth.summary.triad_gb_s.mean"]


def test_quick_profile_caps_the_gpu_matrix_size() -> None:
    quick, _ = plan_cases(CAPS_TORCH_CUDA, sizing.QUICK, cpu_count=4, memory_bytes=1)
    full, _ = plan_cases(CAPS_TORCH_CUDA, sizing.FULL, cpu_count=4, memory_bytes=1)
    quick_gpu = [p for p in quick if p.spec["kind"] == "gpu_matmul"]
    full_gpu = [p for p in full if p.spec["kind"] == "gpu_matmul"]
    assert quick_gpu and full_gpu
    assert all(p.spec["max_n"] == sizing.MAX_GPU_N_QUICK for p in quick_gpu)
    assert all(p.spec["max_n"] == sizing.MAX_GPU_N for p in full_gpu)
    copy = next(p for p in quick if p.case_id == "gpu-copy")
    assert copy.spec["bytes"] == sizing.QUICK_MEMORY_BYTES


def test_expired_budget_before_a_case_is_budget_exhausted_not_a_timeout() -> None:
    """N5: no child is started and the failure names the real cause."""
    plans, _ = plan_cases(
        CAPS_NO_TORCH, sizing.QUICK, cpu_count=2, memory_bytes=2**20, skip_gpu=True
    )
    cfg = RunConfig(profile=sizing.QUICK)
    now = [100.0]
    budget = Budget(5.0, clock=lambda: now[0])
    now[0] += 6.0
    assert budget.expired()
    messages: list[str] = []
    case = execute_case(plans[0], cfg, budget, log=messages.append)
    doc = case.to_json()
    assert doc["aborted"] is True
    assert doc["samples"] == []
    assert [e["failure_type"] for e in doc["errors"]] == ["budget_exhausted"]
    assert "not started" in doc["errors"][0]["message"]
    assert "worker_timeout" not in json.dumps(doc)
    assert messages and "budget" in messages[0]


def test_run_config_defaults_come_from_the_profile() -> None:
    cfg = RunConfig(profile=sizing.FULL)
    assert cfg.effective_reps() == sizing.FULL.reps
    assert cfg.effective_warmup() == sizing.FULL.warmup
    assert cfg.effective_budget() == sizing.FULL.budget_s
    override = RunConfig(profile=sizing.QUICK, reps=9, warmup=0, budget_s=30.0)
    assert (override.effective_reps(), override.effective_warmup()) == (9, 0)
    assert override.effective_budget() == 30.0


def test_budget_counts_down_and_slices_timeouts() -> None:
    now = [1000.0]
    budget = Budget(10.0, clock=lambda: now[0])
    assert budget.remaining() == pytest.approx(10.0)
    assert budget.slice_for(30.0) == pytest.approx(10.0)
    assert budget.slice_for(2.0) == pytest.approx(2.0)
    now[0] += 9.5
    assert not budget.expired()
    assert budget.slice_for(5.0) == pytest.approx(0.5)
    now[0] += 1.0
    assert budget.expired()
    assert budget.slice_for(5.0) == 0.0


def test_failed_case_records_the_setup_error_and_is_aborted() -> None:
    plans, _ = plan_cases(
        CAPS_NO_TORCH, sizing.QUICK, cpu_count=2, memory_bytes=2**20, skip_gpu=True
    )
    cfg = RunConfig(profile=sizing.QUICK)
    case = _failed_case(
        plans[0], cfg, RepError("cuda_broken", "driver exploded", fatal=True)
    )
    doc = case.to_json()
    assert doc["aborted"] is True
    assert doc["samples"] == []
    assert doc["errors"][0]["failure_type"] == "cuda_broken"
    assert doc["errors"][0]["phase"] == "setup"
    assert doc["summary"][next(iter(case.metrics))]["n"] == 0


def test_worker_timeout_after_budget_ran_out_is_budget_exhausted() -> None:
    """A child killed by a truncated slice because ``--timeout`` ran out is the
    operator's budget, not a benchmark hang: the error must say so."""
    from spikes._s7 import run as run_mod
    from spikes._s7.worker import WorkerError

    class _Expired:
        total_s = 1.0

        def expired(self) -> bool:
            return True

    class _Alive(_Expired):
        def expired(self) -> bool:
            return False

    exc = WorkerError("worker_timeout", "no reply within 0.1 s")
    err = run_mod._budget_aware(exc, _Expired())  # type: ignore[arg-type]
    assert err.failure_type == "budget_exhausted"
    assert err.fatal is True

    err = run_mod._budget_aware(exc, _Alive())  # type: ignore[arg-type]
    assert err.failure_type == "worker_timeout"

    other = WorkerError("bad_answer", "garbage")
    assert run_mod._budget_aware(other, _Expired()).failure_type == "bad_answer"  # type: ignore[arg-type]
    assert run_mod._budget_aware(other, _Expired(), fatal=True).fatal is True  # type: ignore[arg-type]
