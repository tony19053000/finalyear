"""V0.15 G4 (D336, D345, D363): the planner's use of an ML term predictor.

* every precedence path, per term and per candidate: ML used, ``no_model``,
  ``ood``, ``interval_too_wide``, ``exact_by_construction``,
  ``below_min_predicted``, ``predictor_error`` - and each fallback is the
  **calibrated** term (never the raw one when a factor exists);
* an OOD node keeps calibration while the other nodes of the same decision
  use the model;
* D363: a raising / malformed / too-slow predictor -> the whole decision is
  the calibrated V0.14 decision plus ``detail.ml.reason = predictor_error``
  and ``Decision.ml.error``; the plan never fails;
* batching: one ``predict`` call per Mode 1 decision, two per Mode 3
  decision (lanes, then combines);
* records: ``ml_model_version`` on every candidate row, nothing without a
  model.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest
from mf_ml_stub import Rule, StubPredictor, stub_ml
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.ml.predictor import BudgetedPredictor, ml_input
from meshforge.controller.ml.schema import TERM_NAMES
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    Calibration,
    Factor,
    TermFactors,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import (
    ML_TERMS,
    MlInput,
    PredictorFailed,
    TermContext,
    stored_raw,
)
from meshforge.controller.planner.replay import project
from meshforge.controller.planner.types import CandidateEval, Decision

pytestmark = pytest.mark.unit

LN_HALF = math.log(0.5)


def _inputs(split: bool = False, n: int = 4) -> tuple[Any, Any, Any]:
    request, snapshot, now = synthetic_fabric(n, split=split, seed=n)
    return request, project(request, snapshot), now


def _calibration(factor: float = 2.0) -> Calibration:
    one = Factor(factor=factor, n=10, median=factor)
    return Calibration(
        version=7,
        terms={
            name: TermFactors(global_=one)
            for name in ("transfer", "env", "compute", "output")
        },
    )


def _row(decision: Decision, node_id: str) -> CandidateEval:
    return next(
        c
        for c in decision.candidates
        if c.node_id == node_id and c.feasible and not c.node_set
    )


def _feasible(decision: Decision) -> list[CandidateEval]:
    return [c for c in decision.candidates if c.feasible and c.estimate is not None]


def test_the_planner_and_the_dataset_name_the_same_terms() -> None:
    assert ML_TERMS == TERM_NAMES


# ------------------------------------------------------------------ Mode 1


def test_ml_used_corrects_raw_and_skips_calibration() -> None:
    request, view, now = _inputs()
    cal = _calibration(2.0)
    base = plan(request, view, "cost_model", now)  # raw
    ml, stub = stub_ml({("compute", None): Rule(y_hat=LN_HALF)})
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    assert stub.calls == 1
    for row in _feasible(decision):
        raw = _row(base, row.node_id).estimate
        assert raw is not None and row.estimate is not None
        compute = row.estimate.compute
        assert compute.seconds == pytest.approx(raw.compute.seconds * 0.5)
        block = compute.detail["ml"]
        assert block["used"] is True
        assert block["model_version"] == "stub-v1"
        assert block["raw_s"] == stored_raw("compute", raw.compute.seconds)
        assert block["y_hat"] == pytest.approx(LN_HALF)
        assert "calibration" not in compute.detail  # ML wins over calibration
        # env has no model -> calibrated (x2), reason no_model
        env = row.estimate.env
        assert env.detail["ml"] == {
            "model_version": "stub-v1",
            "used": False,
            "reason": "no_model",
        }
        assert env.seconds == pytest.approx(raw.env.seconds * 2.0)
    assert decision.ml is not None and decision.ml.version == "stub-v1"
    assert decision.ml.error is None


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        (Rule(y_hat=LN_HALF, ood=True, ood_reason="range:hw_cpu_gflops"), "ood"),
        (Rule(y_hat=LN_HALF, half_width=0.5), "interval_too_wide"),
    ],
)
def test_untrusted_predictions_fall_back_to_calibration(
    rule: Rule, reason: str
) -> None:
    request, view, now = _inputs()
    cal = _calibration(2.0)
    calibrated = plan(request, view, "cost_model", now, calibration=cal)
    ml, _ = stub_ml({("compute", None): rule})
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    for row in _feasible(decision):
        assert row.estimate is not None
        want = _row(calibrated, row.node_id).estimate
        assert want is not None
        compute = row.estimate.compute
        assert compute.seconds == want.compute.seconds
        assert compute.detail["calibration"] == want.compute.detail["calibration"]
        assert compute.detail["ml"]["used"] is False
        assert compute.detail["ml"]["reason"] == reason
        assert compute.detail["ml"]["y_hat"] == pytest.approx(LN_HALF)
        if reason == "ood":
            assert compute.detail["ml"]["ood_reason"] == "range:hw_cpu_gflops"


def test_the_interval_limit_is_inclusive_and_configurable() -> None:
    request, view, now = _inputs()
    ml, _ = stub_ml(
        {("compute", None): Rule(y_hat=0.1, half_width=0.25)}, max_interval_log=0.5
    )
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert all(
        r.estimate is not None and r.estimate.compute.detail["ml"]["used"]
        for r in _feasible(decision)
    )
    narrow = dataclasses.replace(ml, max_interval_log=0.49)
    decision = plan(request, view, "cost_model", now, ml=narrow)
    assert all(
        r.estimate is not None
        and r.estimate.compute.detail["ml"]["reason"] == "interval_too_wide"
        for r in _feasible(decision)
    )


def test_exact_by_construction_terms_never_reach_the_model() -> None:
    request, view, now = _inputs()
    ml, stub = stub_ml({("transfer", None): Rule(y_hat=3.0)})
    decision = plan(request, view, "cost_model", now, ml=ml)
    holders = {"node_000", "node_003"}  # hold the job's tree (synthetic_fabric)
    for row in _feasible(decision):
        assert row.estimate is not None
        transfer = row.estimate.transfer
        if row.node_id in holders:
            assert transfer.detail["missing_bytes"] == 0
            assert transfer.detail["ml"]["reason"] == "exact_by_construction"
            assert transfer.seconds == 0.0
        else:
            assert transfer.detail["ml"]["used"] is True
    asked = {q.node_id for q in stub.queries if q.term == "transfer"}
    assert asked and not asked & holders


@pytest.mark.parametrize(("output_bytes", "asked"), [(1e3, False), (1e9, True)])
def test_a_term_below_the_dataset_floor_is_not_corrected(
    output_bytes: float, asked: bool
) -> None:
    request, view, now = _inputs()
    request = dataclasses.replace(
        request,
        history=dataclasses.replace(
            request.history, output_bytes=output_bytes, output_samples=3
        ),
    )
    ml, stub = stub_ml({("output", None): Rule(y_hat=2.0)})
    decision = plan(request, view, "cost_model", now, ml=ml)
    for row in _feasible(decision):
        assert row.estimate is not None
        output = row.estimate.output
        if asked:
            assert output.detail["ml"]["used"] is True
        else:
            assert output.seconds < 0.05
            assert output.detail["ml"]["reason"] == "below_min_predicted"
    assert any(q.term == "output" for q in stub.queries) is asked


def test_an_ood_node_keeps_calibration_while_the_others_use_ml() -> None:
    request, view, now = _inputs()
    cal = _calibration(1.5)
    ml, _ = stub_ml(
        {
            ("compute", None): Rule(y_hat=LN_HALF),
            ("compute", "node_002"): Rule(y_hat=LN_HALF, ood=True, ood_reason="knn"),
        }
    )
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    used = {
        r.node_id: r.estimate.compute.detail["ml"]["used"]
        for r in _feasible(decision)
        if r.estimate is not None
    }
    assert used.pop("node_002") is False
    assert used and all(used.values())
    ood = _row(decision, "node_002").estimate
    assert ood is not None and ood.compute.detail["calibration"]["factor"] == 1.5


# ------------------------------------------------------------------ D363


def _assert_fell_back(decision: Decision, calibrated: Decision, error: str) -> None:
    assert decision.ml is not None and decision.ml.error == error
    assert decision.chosen is not None and calibrated.chosen is not None
    assert decision.chosen.node_id == calibrated.chosen.node_id
    for row in _feasible(decision):
        want = _row(calibrated, row.node_id).estimate
        assert row.estimate is not None and want is not None
        assert row.estimate.total_s == want.total_s
        for name in ("transfer", "env", "compute", "output"):
            got = getattr(row.estimate, name)
            assert got.seconds == getattr(want, name).seconds
            assert got.detail["ml"]["used"] is False
            assert got.detail["ml"]["reason"] in {
                "predictor_error",
                "exact_by_construction",
                "below_min_predicted",
            }
    rows = candidates_json(decision)
    assert all(r["ml_fallback"] == "predictor_error" for r in rows)


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_a_raising_predictor_falls_back_to_calibration(split: bool) -> None:
    request, view, now = _inputs(split=split)
    cal = _calibration(1.3)
    calibrated = plan(request, view, "cost_model", now, calibration=cal)
    ml, stub = stub_ml(
        {(t, None): Rule(y_hat=LN_HALF) for t in ML_TERMS},
        raises=RuntimeError("model file vanished"),
    )
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    assert stub.calls == 1  # the fallback never asks again
    if split:
        assert decision.ml is not None and decision.ml.error == "RuntimeError"
        assert decision.split == calibrated.split
    else:
        _assert_fell_back(decision, calibrated, "RuntimeError")


@pytest.mark.parametrize(
    "broken",
    [
        {"answer_count_delta": 1},
        {"answer_count_delta": -1},
        {"rules": {("compute", None): Rule(y_hat=math.nan)}},
        {"rules": {("compute", None): Rule(y_hat=math.inf)}},
        {"rules": {("compute", None): Rule(y_hat=0.0, half_width=-1.0)}},
        {"rules": {("compute", None): Rule(y_hat=900.0)}},
    ],
    ids=["one_more", "one_less", "nan", "inf", "lo_above_hi", "absurd"],
)
def test_a_malformed_answer_falls_back(broken: dict[str, Any]) -> None:
    request, view, now = _inputs()
    cal = _calibration(1.3)
    calibrated = plan(request, view, "cost_model", now, calibration=cal)
    rules = broken.pop("rules", {("compute", None): Rule(y_hat=0.1)})
    ml, _ = stub_ml(rules, **broken)
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    _assert_fell_back(decision, calibrated, "malformed_prediction")


def test_a_feature_builder_error_falls_back() -> None:
    request, view, now = _inputs()
    stub = StubPredictor(rules={("compute", None): Rule()})

    def broken(*_args: Any) -> tuple[float, ...]:
        raise ZeroDivisionError

    ml = MlInput(predictor=stub, features=broken)
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.ml is not None and decision.ml.error == "ZeroDivisionError"
    assert stub.calls == 0


def test_a_too_slow_predictor_falls_back_with_budget_exceeded() -> None:
    request, view, now = _inputs()
    cal = _calibration(1.3)
    calibrated = plan(request, view, "cost_model", now, calibration=cal)
    stub = StubPredictor(rules={("compute", None): Rule(y_hat=LN_HALF)})
    ticks = iter([0.0, 0.0201])  # 20.1 ms
    ml = ml_input(stub, budget_ms=20.0, clock=lambda: next(ticks))
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    _assert_fell_back(decision, calibrated, "budget_exceeded")
    # inside the budget the same model is used
    ticks2 = iter([0.0, 0.0199])
    ok = ml_input(stub, budget_ms=20.0, clock=lambda: next(ticks2))
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ok)
    assert decision.ml is not None and decision.ml.error is None


def test_budgeted_predictor_refuses_a_bad_budget() -> None:
    stub = StubPredictor(rules={})
    for bad in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(ValueError, match="budget"):
            BudgetedPredictor(stub, bad)


def test_predictor_failed_names_its_cause() -> None:
    assert PredictorFailed("x").cause == "x"


# ------------------------------------------------------------------ records


def test_every_row_names_the_model_version_and_none_without_a_model() -> None:
    for split in (False, True):
        request, view, now = _inputs(split=split)
        plain = candidates_json(plan(request, view, "cost_model", now))
        assert all(
            "ml_model_version" not in r and "ml_fallback" not in r for r in plain
        )
        ml, _ = stub_ml({("compute", None): Rule()})
        rows = candidates_json(plan(request, view, "cost_model", now, ml=ml))
        assert rows and all(r["ml_model_version"] == "stub-v1" for r in rows)
        assert all("ml_fallback" not in r for r in rows)


def test_the_digest_names_the_model() -> None:
    request, view, now = _inputs()
    plain = plan(request, view, "cost_model", now)
    ml, _ = stub_ml({("compute", None): Rule()})
    with_ml = plan(request, view, "cost_model", now, ml=ml)
    other = dataclasses.replace(ml, predictor=StubPredictor(version="stub-v2"))
    with_other = plan(request, view, "cost_model", now, ml=other)
    assert (
        len(
            {plain.snapshot_digest, with_ml.snapshot_digest, with_other.snapshot_digest}
        )
        == 3
    )


def test_no_model_for_any_term_changes_only_the_ml_records() -> None:
    """A predictor with no model at all: the same times, ranks and pick as the
    calibrated planner (every term ``no_model``)."""
    for split in (False, True):
        request, view, now = _inputs(split=split)
        cal = _calibration(1.2)
        calibrated = plan(request, view, "cost_model", now, calibration=cal)
        ml, stub = stub_ml({})
        decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
        assert stub.calls == 0
        assert [
            (c.node_id, c.node_set, c.rank, c.score) for c in decision.candidates
        ] == [(c.node_id, c.node_set, c.rank, c.score) for c in calibrated.candidates]
        assert decision.split == calibrated.split


# ------------------------------------------------------------------ batching


def test_mode1_asks_the_model_once_for_every_candidate_and_term() -> None:
    request, view, now = _inputs(n=6)
    ml, stub = stub_ml(
        {(t, None): Rule() for t in ("transfer", "env", "compute", "output")}
    )
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert stub.calls == 1
    nodes = {q.node_id for q in stub.queries}
    assert nodes == {r.node_id for r in _feasible(decision)}


def test_mode3_asks_twice_lanes_then_combines() -> None:
    request, view, now = _inputs(split=True, n=4)
    ml, stub = stub_ml({(t, None): Rule() for t in ML_TERMS})
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert stub.calls == 2
    terms = [q.term for q in stub.queries]
    lanes = {"lane_stage", "lane_env", "chunk_rate"}
    first = [t for t in terms if t in lanes]
    assert set(terms) <= lanes | {"combine"}
    assert terms[: len(first)] == first  # lanes first, then the combines
    combines = [q for q in stub.queries if q.term == "combine"]
    assert len(combines) == sum(1 for c in _feasible(decision))


def test_term_context_defaults_describe_a_whole_job() -> None:
    ctx = TermContext(term="compute", node_id="n", raw_s=1.0, source="history")
    assert (ctx.lanes, ctx.chunk_items, dict(ctx.detail)) == (1, None, {})
