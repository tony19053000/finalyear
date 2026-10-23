"""V0.15 G5b (D374): the predictions read's pure part
(:func:`meshforge.controller.ml.predictions.points_of`).

Decisions are the real planner's stored rows (``mf_ml_fixtures``) with
``detail.ml`` blocks set as G4 writes them. Each point must carry the
dataset's raw (analytical) value and actual, ``ml_s = raw x e^y_hat`` only
where ML was used, ``calibrated_s`` = the stored factor, or - for a term ML
corrected - the decision's calibration version re-applied (null when that
version cannot be read), and the decision's model set.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest
from mf_ml_fixtures import chosen, lane_row, set_run, stored, whole_run

from meshforge.controller.ml.predictions import points_of
from meshforge.controller.ml.targets import observations
from meshforge.controller.planner.calibration import Calibration, Factor, TermFactors
from meshforge.controller.planner.predictor import ML_TERMS

pytestmark = pytest.mark.unit

PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}
SET = "sha256:" + "ab" * 32


def _ml_whole(*, calibrated_env: bool = True) -> Any:
    s = stored()
    s = copy.deepcopy(s)
    for row in s.candidates:
        row["ml_model_version"] = SET
    terms = chosen(s)["estimate"]["terms"]
    raw = terms["compute"]["seconds"]
    terms["compute"]["detail"]["ml"] = {
        "model_version": "m1",
        "used": True,
        "y_hat": -0.2,
        "lo": -0.3,
        "hi": -0.1,
        "raw_s": raw,
    }
    terms["compute"]["seconds"] = raw * math.exp(-0.2)
    transfer = terms["env"]
    if calibrated_env:
        transfer["detail"]["calibration"] = {
            "factor": 2.0,
            "n": 9,
            "key_level": "global",
            "version": 3,
            "raw_s": transfer["seconds"],
        }
        transfer["seconds"] = transfer["seconds"] * 2.0
    transfer["detail"]["ml"] = {
        "model_version": SET,
        "used": False,
        "reason": "ood",
        "y_hat": 0.3,
        "lo": 0.0,
        "hi": 0.6,
    }
    return s


def _calibration(factor: float = 1.5) -> Calibration:
    return Calibration(
        version=3,
        terms={"compute": TermFactors(global_=Factor(factor=factor, n=20, median=1.4))},
    )


def _by_term(points: list[Any]) -> dict[str, Any]:
    return {p.term: p for p in points}


def test_points_equal_the_dataset_rows_with_the_ml_and_calibration_values() -> None:
    s = _ml_whole()
    run = whole_run(s, phases=PHASES, decision_id=5)
    rows, _ = observations(run)
    points = points_of([run], versions={5: 3}, calibrations={3: _calibration()})
    assert [p.term for p in points] == [o.term for o in rows]
    by = _by_term(points)
    for obs in rows:
        p = by[obs.term]
        assert p.analytical_s == obs.raw_s
        assert p.actual_s == obs.actual_s
        assert p.node_id == obs.node_id
        assert p.decision_id == 5
        assert p.family == "cpu.primes"
        assert p.ml_model_version == SET
    compute = by["compute"]
    assert compute.used is True and compute.reason is None
    assert compute.ml_s == pytest.approx(compute.analytical_s * math.exp(-0.2))
    # ML corrected compute, so its stored row has no factor: the decision's
    # calibration v3 is re-applied (global x1.5)
    assert compute.calibrated_s == pytest.approx(compute.analytical_s * 1.5)
    transfer = by["env"]
    assert transfer.used is False and transfer.reason == "ood"
    assert transfer.ml_s is None
    assert transfer.calibrated_s == pytest.approx(transfer.analytical_s * 2.0)
    output = by["output"]  # no ML block: planned without the term's model
    assert output.used is False and output.reason is None
    assert output.calibrated_s == output.analytical_s


def test_unreadable_calibration_version_gives_null_never_a_guess() -> None:
    run = whole_run(_ml_whole(), phases=PHASES, decision_id=5)
    by = _by_term(points_of([run], versions={5: 3}, calibrations={3: None}))
    assert by["compute"].calibrated_s is None
    # a version that was never read (over the per-request cap) is the same
    by = _by_term(points_of([run], versions={5: 3}, calibrations={}))
    assert by["compute"].calibrated_s is None


def test_uncalibrated_decision_compares_ml_with_the_analytical_value() -> None:
    run = whole_run(_ml_whole(), phases=PHASES, decision_id=5)
    by = _by_term(points_of([run], versions={5: None}, calibrations={}))
    assert by["compute"].calibrated_s == by["compute"].analytical_s


def test_term_and_set_filters() -> None:
    ml_run = whole_run(_ml_whole(), phases=PHASES, decision_id=5)
    plain = whole_run(stored(seed=1), phases=PHASES, decision_id=6)
    both = [ml_run, plain]
    only = points_of(both, versions={}, calibrations={}, term="compute")
    assert {p.term for p in only} == {"compute"}
    assert {p.decision_id for p in only} == {5, 6}
    in_set = points_of(both, versions={}, calibrations={}, set_version=SET)
    assert {p.decision_id for p in in_set} == {5}
    other = points_of(
        both, versions={}, calibrations={}, set_version="sha256:" + "0" * 64
    )
    assert other == []


def test_a_decision_without_ml_has_no_ml_fields() -> None:
    run = whole_run(stored(seed=2), phases=PHASES, decision_id=9)
    points = points_of([run], versions={}, calibrations={})
    assert points
    for p in points:
        assert p.used is False and p.reason is None and p.ml_s is None
        assert p.ml_model_version is None


def test_mode3_chunk_rate_reads_the_lane_rows_nested_rate_block() -> None:
    s = copy.deepcopy(stored(forced_set=True))
    for row in s.candidates:
        row["ml_model_version"] = SET
    node = chosen(s)["node_set"][0]
    compute = lane_row(s, node)["estimate"]["terms"]["compute"]
    compute["detail"]["ml"] = {
        "model_version": "m1",
        "used": True,
        "y_hat": 0.1,
        "lo": 0.0,
        "hi": 0.2,
        "raw_s": compute["seconds"],
        "rate": {
            "model_version": "m1",
            "used": True,
            "y_hat": 0.1,
            "lo": 0.0,
            "hi": 0.2,
            "raw_s": 0.05,
        },
    }
    run = set_run(s)
    points = points_of([run], versions={}, calibrations={})
    rates = {p.node_id: p for p in points if p.term == "chunk_rate"}
    assert node in rates
    mine = rates[node]
    assert mine.used is True
    assert mine.analytical_s == pytest.approx(0.05)
    assert mine.ml_s == pytest.approx(0.05 * math.exp(0.1))
    assert mine.calibrated_s == mine.analytical_s  # a rate is never calibrated
    others = [p for n, p in rates.items() if n != node]
    assert others and all(p.used is False and p.ml_s is None for p in others)
    terms = {p.term for p in points}
    assert {"lane_stage", "chunk_rate", "combine"} <= terms


def test_the_route_literal_lists_exactly_the_learnable_terms() -> None:
    import typing

    from meshforge.controller.api.ml_prediction_routes import MlTerm

    assert set(typing.get_args(MlTerm.__value__)) == set(ML_TERMS)
