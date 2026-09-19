"""Unit tests for the S7 worker helpers (0.2.11): no subprocess is started here.

Spawning real children is covered by ``tests/component/lab/test_s7_hardware.py``.
"""

from __future__ import annotations

import pytest

from spikes._s7.worker import (
    THREAD_VARS,
    Call,
    WorkerError,
    WorkerGroup,
    merge_samples,
    run_parallel,
    thread_env,
)


def test_thread_env_pins_every_blas_variable() -> None:
    env = thread_env(4)
    assert set(env) == set(THREAD_VARS)
    assert set(env.values()) == {"4"}
    with pytest.raises(ValueError):
        thread_env(0)


def test_merge_of_one_worker_is_the_sample_itself() -> None:
    sample = {"gflops": 12.5, "elapsed_s": 0.4}
    merged = merge_samples([sample])
    assert merged == sample
    assert merged is not sample  # a copy, so the caller cannot mutate the original


def test_merge_sums_rates_and_takes_the_slowest_duration() -> None:
    merged = merge_samples(
        [
            {"python_mflops": 30.0, "elapsed_s": 0.30},
            {"python_mflops": 28.0, "elapsed_s": 0.35},
            {"python_mflops": 31.0, "elapsed_s": 0.31},
        ]
    )
    assert merged["python_mflops"] == pytest.approx(89.0)
    assert merged["elapsed_s"] == pytest.approx(0.35)
    assert merged["workers"] == 3
    assert len(merged["per_worker"]) == 3


def test_merge_skips_metrics_a_worker_did_not_report() -> None:
    merged = merge_samples(
        [{"a_gb_s": 1.0, "elapsed_s": 1.0}, {"elapsed_s": 2.0, "note": "x"}]
    )
    assert "a_gb_s" not in merged  # not every worker had it: never a partial sum
    assert merged["elapsed_s"] == 2.0
    assert "note" not in merged


def test_merge_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError):
        merge_samples([])


def test_run_parallel_collects_results_and_errors() -> None:
    def ok() -> dict[str, float]:
        return {"x": 1.0}

    def bad() -> dict[str, float]:
        raise WorkerError("child_crashed", "boom", fatal=True)

    calls: list[Call] = [ok, bad, ok]
    results, errors = run_parallel(calls, 5.0)
    assert len(results) == 2
    assert [e.failure_type for e in errors] == ["child_crashed"]
    assert errors[0].fatal is True


def test_worker_group_rejects_an_empty_group() -> None:
    with pytest.raises(ValueError):
        WorkerGroup(0)


def test_worker_error_carries_type_and_fatality() -> None:
    exc = WorkerError("worker_timeout", "no answer", fatal=True)
    assert (exc.failure_type, exc.fatal, str(exc)) == ("worker_timeout", True, "no answer")
    assert WorkerError("bad_answer", "x").fatal is False
