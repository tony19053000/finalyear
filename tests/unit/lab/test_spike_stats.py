"""Unit tests for the shared spike harness: statistics and repetition runner (0.2.5)."""

from __future__ import annotations

import math
from collections.abc import Iterator

import pytest

from spikes._harness import MIN_VALID_REPS
from spikes._harness.reps import RepError, Sample, run_reps
from spikes._harness.stats import (
    Summary,
    mean_abs_delta,
    quantile,
    rfc3550_jitter,
    summarize,
)


def test_summarize_known_values() -> None:
    s = summarize([5, 1, 3, 2, 4])
    assert s.n == 5
    assert s.mean == pytest.approx(3.0)
    assert s.stdev == pytest.approx(math.sqrt(2.5))  # sample stdev (n - 1)
    assert (s.min, s.max, s.median) == (1.0, 5.0, 3.0)
    assert s.p95 == pytest.approx(4.8)  # linear interpolation: 4 + 0.8 * (5 - 4)
    assert s.valid


def test_summarize_single_and_empty() -> None:
    one = summarize([7.5])
    assert (one.n, one.mean, one.stdev, one.p95) == (1, 7.5, None, 7.5)
    assert not one.valid
    empty = summarize([])
    assert empty == Summary(0, None, None, None, None, None, None)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_summarize_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        summarize([1.0, bad])


def test_quantile_edges() -> None:
    assert quantile([1.0, 2.0], 0.0) == 1.0
    assert quantile([1.0, 2.0], 1.0) == 2.0
    assert quantile([1.0, 2.0], 0.5) == 1.5
    with pytest.raises(ValueError):
        quantile([], 0.5)
    with pytest.raises(ValueError):
        quantile([1.0], 1.5)


def test_summary_json_round_trip_and_validation() -> None:
    s = summarize([1, 2, 3, 4, 5, 6])
    assert Summary.from_json(s.to_json()) == s
    with pytest.raises(TypeError):
        Summary.from_json({"n": "5"})
    with pytest.raises(TypeError):
        Summary.from_json({"n": True})
    with pytest.raises(ValueError):
        Summary.from_json({"n": -1})
    with pytest.raises(TypeError):
        Summary.from_json({"n": 2, "mean": "fast"})


def test_jitter_estimators() -> None:
    assert mean_abs_delta([1.0, 3.0, 2.0]) == pytest.approx(1.5)
    assert mean_abs_delta([4.0]) is None
    assert rfc3550_jitter([0.0, 16.0]) == pytest.approx(1.0)  # J += (|D| - J) / 16
    assert rfc3550_jitter([2.0, 2.0, 2.0]) == 0.0
    assert rfc3550_jitter([1.0]) is None


def test_min_valid_reps_is_five() -> None:
    assert MIN_VALID_REPS == 5


# ------------------------------------------------------------------ run_reps
def _const(values: dict[int, float | RepError]) -> object:
    def measure(index: int) -> Sample:
        value = values.get(index, 1.0)
        if isinstance(value, RepError):
            raise value
        return {"x": value}

    return measure


def test_warmup_is_kept_but_excluded_from_summary() -> None:
    result = run_reps(
        "c",
        _const({0: 1000.0}),  # type: ignore[arg-type]
        reps=5,
        warmup=1,
        metrics={"x": "u"},
    )
    assert len(result.warmup) == 1 and result.warmup[0]["x"] == 1000.0
    assert len(result.samples) == 5
    summary = result.summary()["x"]
    assert summary.n == 5 and summary.mean == 1.0
    doc = result.to_json()
    assert doc["summary"]["x"]["n"] == 5
    assert doc["requested_reps"] == 5 and doc["requested_warmup"] == 1


def test_non_fatal_error_is_recorded_and_run_continues() -> None:
    result = run_reps(
        "c",
        _const({2: RepError("measurement_invalid", "odd")}),  # type: ignore[arg-type]
        reps=5,
        warmup=1,
        metrics={"x": "u"},
    )
    assert len(result.samples) == 4
    assert result.errors == [
        {
            "rep": 2,
            "phase": "measure",
            "failure_type": "measurement_invalid",
            "message": "odd",
            "fatal": False,
        }
    ]
    assert not result.aborted
    assert not result.summary()["x"].valid  # 4 < 5


def test_fatal_error_aborts_the_case() -> None:
    calls: list[int] = []

    def measure(index: int) -> Sample:
        calls.append(index)
        if index == 3:
            raise RepError("connection_lost", "gone", fatal=True)
        return {"x": 1.0}

    result = run_reps("c", measure, reps=5, warmup=1, metrics={"x": "u"})
    assert calls == [0, 1, 2, 3]
    assert result.aborted
    assert len(result.samples) == 2


def test_unexpected_exceptions_propagate_and_bad_args_rejected() -> None:
    def boom(_index: int) -> Sample:
        raise KeyError("bug")

    with pytest.raises(KeyError):
        run_reps("c", boom, reps=5, warmup=0, metrics={"x": "u"})
    with pytest.raises(ValueError):
        run_reps("c", boom, reps=0, warmup=0, metrics={})
    with pytest.raises(ValueError):
        run_reps("c", boom, reps=1, warmup=-1, metrics={})


def test_non_numeric_metric_values_are_ignored_in_summary() -> None:
    values: Iterator[Sample] = iter([{"x": 1.0}, {"x": None}, {"x": True}, {"x": 3.0}])
    result = run_reps(
        "c", lambda _i: next(values), reps=4, warmup=0, metrics={"x": "u"}
    )
    # None and bool (an int subclass) are not measurements
    assert result.summary()["x"].n == 2
