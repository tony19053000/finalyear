"""D462 tester: planner-side adversarial tests (not_approved source)."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import Calibration, Factor, TermFactors
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import project

pytestmark = pytest.mark.unit


def _inputs(split: bool = False) -> tuple[Any, Any, Any]:
    request, snapshot, now = synthetic_fabric(4, split=split, seed=4)
    return request, project(request, snapshot), now


def _cal() -> Calibration:
    one = Factor(factor=2.0, n=10, median=2.0)
    names = ("transfer", "env", "compute", "output")
    return Calibration(version=7, terms={n: TermFactors(global_=one) for n in names})


def _mls(obj: Any, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(obj, dict):
        if isinstance(obj.get("ml"), dict) and "used" in obj["ml"]:
            found.append(obj["ml"])
        for v in obj.values():
            _mls(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _mls(v, found)
    return found


def _seconds(decision: Any) -> list[tuple[str, float, float]]:
    return sorted(
        (c.node_id, c.estimate.compute.seconds, c.estimate.env.seconds)
        for c in decision.candidates
        if c.feasible and c.estimate is not None and not c.node_set
    )


def test_withholding_every_term_equals_calibration_only_plan() -> None:
    request, view, now = _inputs()
    cal = _cal()
    ml, stub = stub_ml(
        {("compute", None): Rule(y_hat=-1.0), ("env", None): Rule(y_hat=1.0)}
    )
    ml = replace(ml, withheld=frozenset({"compute", "env"}))
    with_ml = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    without = plan(request, view, "cost_model", now, calibration=cal)
    assert not stub.queries
    assert _seconds(with_ml) == _seconds(without)


def test_withheld_term_not_asked_even_when_model_would_answer_wildly() -> None:
    request, view, now = _inputs()
    ml, stub = stub_ml({("compute", None): Rule(y_hat=5.0)})
    ml = replace(ml, withheld=frozenset({"compute"}))
    d = plan(request, view, "cost_model", now, ml=ml)
    assert not stub.queries
    reasons = {m.get("reason") for m in _mls(candidates_json(d), [])}
    assert "not_approved" in reasons


def test_withheld_term_when_predictor_has_no_model_is_not_approved() -> None:
    request, view, now = _inputs()
    ml, _ = stub_ml({("compute", None): Rule()})
    ml = replace(ml, withheld=frozenset({"env"}))
    d = plan(request, view, "cost_model", now, ml=ml)
    assert any(m.get("reason") == "not_approved" for m in _mls(candidates_json(d), []))


def test_predictor_error_fallback_kept_for_approved_terms() -> None:
    request, view, now = _inputs()
    ml, _ = stub_ml(
        {("compute", None): Rule(), ("env", None): Rule()},
        raises=RuntimeError("boom"),
    )
    ml = replace(ml, withheld=frozenset({"env"}))
    d = plan(request, view, "cost_model", now, ml=ml)
    reasons = {m.get("reason") for m in _mls(candidates_json(d), [])}
    assert "predictor_error" in reasons


def test_interval_too_wide_per_case_for_approved_and_not_approved_for_rest() -> None:
    request, view, now = _inputs()
    ml, _ = stub_ml(
        {("compute", None): Rule(half_width=50.0), ("env", None): Rule()},
        max_interval_log=0.5,
    )
    ml = replace(ml, withheld=frozenset({"env"}))
    d = plan(request, view, "cost_model", now, ml=ml)
    mls = _mls(candidates_json(d), [])
    assert any(m.get("reason") == "interval_too_wide" for m in mls)
    assert any(m.get("reason") == "not_approved" for m in mls)
    assert not any(m.get("used") for m in mls)


def test_plan_is_deterministic_with_withheld() -> None:
    request, view, now = _inputs()
    ml, _ = stub_ml({("compute", None): Rule(y_hat=0.2), ("env", None): Rule()})
    ml = replace(ml, withheld=frozenset({"env"}))
    a = plan(request, view, "cost_model", now, ml=ml)
    b = plan(request, view, "cost_model", now, ml=ml)
    assert candidates_json(a) == candidates_json(b)


def test_cost_formula_unchanged_ml_used_is_raw_times_exp_yhat() -> None:
    request, view, now = _inputs()
    base = plan(request, view, "cost_model", now)
    ml, _ = stub_ml(
        {("compute", None): Rule(y_hat=math.log(3.0)), ("env", None): Rule()}
    )
    ml = replace(ml, withheld=frozenset({"env"}))
    d = plan(request, view, "cost_model", now, ml=ml)
    for row in d.candidates:
        if not (row.feasible and row.estimate and not row.node_set):
            continue
        raw = next(
            c
            for c in base.candidates
            if c.node_id == row.node_id and c.feasible and not c.node_set
        ).estimate
        assert row.estimate.compute.seconds == pytest.approx(raw.compute.seconds * 3.0)
        assert row.estimate.env.seconds == raw.env.seconds


def test_split_plan_withheld_terms_are_not_approved_and_never_asked() -> None:
    request, view, now = _inputs(split=True)
    names = ("compute", "env", "combine", "lane_env", "chunk_rate")
    ml, stub = stub_ml({(t, None): Rule() for t in names})
    withheld = frozenset({"compute", "chunk_rate", "lane_env"})
    ml = replace(ml, withheld=withheld)
    d = plan(request, view, "cost_model", now, ml=ml)
    assert not {q.term for q in stub.queries} & withheld
    assert any(m.get("reason") == "not_approved" for m in _mls(candidates_json(d), []))
