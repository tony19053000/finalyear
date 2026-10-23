"""V0.15 G5a step 6 (G3/G4 reviewer tech debt) in the planner.

* ``MlSession`` builds one feature row per distinct term context within a
  decision (a repeated context is not rebuilt; an unkeyable detail is never
  cached, so it can never be served a wrong row);
* a ``combine`` term whose detail names no combine node is never sent to the
  model: reason ``combine_node_unknown`` (no silent guess of the node), with
  ``predictor_error`` / ``no_model`` taking precedence.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.planner import plan
from meshforge.controller.planner import split as split_module
from meshforge.controller.planner.predictor import (
    ML_TERMS,
    MlInput,
    MlReason,
    MlSession,
    TermContext,
)
from meshforge.controller.planner.replay import project
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Decision,
    EstimateTerm,
    PlanRequest,
)

pytestmark = pytest.mark.unit


def _inputs(split: bool = False, n: int = 4) -> tuple[Any, Any, Any]:
    request, snapshot, now = synthetic_fabric(n, split=split, seed=n)
    return request, project(request, snapshot), now


class _Counting:
    """A feature builder that counts its calls (and returns a fixed row)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        request: PlanRequest,
        snapshot: ClusterSnapshot,
        now: dt.datetime | None,
        ctx: TermContext,
    ) -> tuple[float, ...]:
        self.calls += 1
        return (float(self.calls),)


def _session(features: _Counting) -> tuple[MlSession, Any]:
    ml, stub = stub_ml({("compute", None): Rule(y_hat=0.1)})
    request, view, now = _inputs()
    ml = MlInput(predictor=stub, features=features)
    return MlSession(ml, request, view, now), stub


def test_a_repeated_term_context_builds_its_features_once() -> None:
    features = _Counting()
    session, stub = _session(features)
    a = TermContext("compute", "n1", 10.0, "history", {"samples": 3})
    b = TermContext("compute", "n2", 10.0, "history", {"samples": 3})
    out = session.resolve([a, b, a, dataclasses.replace(a, lanes=2)])
    assert features.calls == 3  # a, b, a with 2 lanes - the second a is cached
    rows = [q.features for q in stub.queries]
    assert rows[0] == rows[2] and rows[0] != rows[1] and rows[3] not in rows[:3]
    assert [o.used for o in out] == [True] * 4
    session.resolve([b])  # still the same decision: b is cached too
    assert features.calls == 3


def test_a_context_whose_detail_cannot_be_keyed_is_built_every_time() -> None:
    features = _Counting()
    session, _stub = _session(features)
    odd = TermContext("compute", "n1", 10.0, "history", {"tags": {1, 2}})
    session.resolve([odd, odd])
    assert features.calls == 2


def test_nested_details_are_keyed_by_value() -> None:
    features = _Counting()
    session, _stub = _session(features)
    one = TermContext("compute", "n1", 1.0, None, {"a": [1, {"b": 2}]})
    same = TermContext("compute", "n1", 1.0, None, {"a": [1, {"b": 2}]})
    other = TermContext("compute", "n1", 1.0, None, {"a": [1, {"b": 3}]})
    session.resolve([one, same, other])
    assert features.calls == 2


# ------------------------------------------------------------ combine node


def _no_combine_node(monkeypatch: pytest.MonkeyPatch) -> None:
    real = split_module._combine

    def without(*args: Any, **kwargs: Any) -> EstimateTerm:
        term = real(*args, **kwargs)
        detail = {k: v for k, v in term.detail.items() if k != "combine_node_id"}
        return EstimateTerm(term.seconds, term.source, detail)

    monkeypatch.setattr(split_module, "_combine", without)


def _combine_blocks(decision: Decision) -> list[dict[str, Any]]:
    return [
        dict(c.estimate.extra["combine"].detail["ml"])
        for c in decision.candidates
        if c.feasible and c.estimate is not None and "combine" in c.estimate.extra
    ]


def test_a_combine_without_its_node_is_not_sent_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_combine_node(monkeypatch)
    request, view, now = _inputs(split=True)
    ml, stub = stub_ml({(t, None): Rule() for t in ML_TERMS})
    decision = plan(request, view, "cost_model", now, ml=ml)
    blocks = _combine_blocks(decision)
    assert blocks
    assert {b["reason"] for b in blocks} == {"combine_node_unknown"}
    assert all(b["used"] is False for b in blocks)
    assert not [q for q in stub.queries if q.term == "combine"]
    assert decision.ml is not None and decision.ml.error is None


def test_without_a_combine_model_the_reason_stays_no_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_combine_node(monkeypatch)
    request, view, now = _inputs(split=True)
    ml, _stub = stub_ml({(t, None): Rule() for t in ML_TERMS if t != "combine"})
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert {b["reason"] for b in _combine_blocks(decision)} == {"no_model"}


def test_a_failed_decision_says_predictor_error_for_the_combine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_combine_node(monkeypatch)
    request, view, now = _inputs(split=True)
    ml, _stub = stub_ml({(t, None): Rule() for t in ML_TERMS}, raises=RuntimeError())
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert {b["reason"] for b in _combine_blocks(decision)} == {"predictor_error"}


def test_with_its_node_the_combine_is_asked_as_before() -> None:
    request, view, now = _inputs(split=True)
    ml, stub = stub_ml({(t, None): Rule() for t in ML_TERMS})
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert {b["used"] for b in _combine_blocks(decision)} == {True}
    assert [q for q in stub.queries if q.term == "combine"]


def test_skip_names_the_reason_only_when_nothing_precedes_it() -> None:
    request, view, now = _inputs()
    ml, _ = stub_ml({("combine", None): Rule()})
    assert (
        MlSession(ml, request, view, now)
        .skip("combine", MlReason.COMBINE_NODE_UNKNOWN)
        .reason
        is MlReason.COMBINE_NODE_UNKNOWN
    )
    assert (
        MlSession(ml, request, view, now)
        .skip("compute", MlReason.COMBINE_NODE_UNKNOWN)
        .reason
        is MlReason.NO_MODEL
    )
    failed = MlSession(ml, request, view, now, failed="boom")
    assert (
        failed.skip("combine", MlReason.COMBINE_NODE_UNKNOWN).reason
        is MlReason.PREDICTOR_ERROR
    )
