"""V0.15 G4 (D364, Q21, Q23): an ML decision's replay.

* the replay gains ``ml: {version}`` (+ ``fallback`` after a predictor
  error); ``REPLAY_VERSION`` stays 2; no model -> no key (a V0.14 replay);
* ``replay_decision`` with the same model reproduces the decision field for
  field (``decision_view``: stored JSON, split plan, digest, every field);
* the model gone -> ``ml_model_unavailable`` and no decision (never a silent
  re-plan with calibration);
* a fallback decision replays without its model, exactly;
* a malformed ``ml`` key is a ``ReplayError``;
* the training side reads the analytical raw back from an ML-corrected row
  (``raw_prediction`` / the lane rate's ``detail.ml.raw_s``).
"""

from __future__ import annotations

import dataclasses
import json
import math
from typing import Any

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import synthetic_fabric
from mf_planner_golden import decision_view

from meshforge.controller.ml.replay import ML_MODEL_UNAVAILABLE, replay_decision
from meshforge.controller.ml.targets import _raw_seconds_per_item
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    Calibration,
    Factor,
    TermFactors,
    raw_prediction,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import ML_TERMS, MlInput
from meshforge.controller.planner.replay import (
    REPLAY_VERSION,
    ReplayError,
    canonical_json,
    ml_from_replay,
    project,
    to_replay,
)
from meshforge.controller.planner.types import MlUse

pytestmark = pytest.mark.unit


def _cal() -> Calibration:
    f = Factor(factor=1.7, n=9, median=1.7)
    return Calibration(
        version=4,
        terms={
            t: TermFactors(global_=f) for t in ("transfer", "env", "compute", "output")
        },
    )


def _stored(replay: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(canonical_json(replay))
    return out


def _case(split: bool, **stub: Any) -> tuple[Any, ...]:
    request, snapshot, now = synthetic_fabric(4, split=split, seed=21)
    view = project(request, snapshot)
    rules = {(t, None): Rule(y_hat=-0.3, half_width=0.05) for t in ML_TERMS}
    rules[("compute", "node_002")] = Rule(y_hat=0.4, ood=True, ood_reason="knn")
    ml, predictor = stub_ml(rules, **stub)
    decision = plan(request, view, "cost_model", now, calibration=_cal(), ml=ml)
    replay = _stored(to_replay(request, view, now, calibration=_cal(), ml=decision.ml))
    return ml, predictor, decision, replay


def test_replay_version_stays_2_and_no_model_writes_no_key() -> None:
    assert REPLAY_VERSION == 2
    request, snapshot, now = synthetic_fabric(4, split=False, seed=21)
    replay = to_replay(request, snapshot, now)
    assert "ml" not in replay
    assert ml_from_replay(replay) is None


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_the_same_model_reproduces_the_decision(split: bool) -> None:
    ml, _, decision, replay = _case(split)
    assert replay["ml"] == {"version": "stub-v1"}
    result = replay_decision(
        replay, "cost_model", load=lambda v: ml if v == "stub-v1" else None
    )
    assert result.status == "ok" and result.ml_model_version == "stub-v1"
    assert result.decision is not None
    assert decision_view(result.decision) == decision_view(decision)


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_a_missing_model_is_reported_never_replanned(split: bool) -> None:
    _, _, _, replay = _case(split)
    result = replay_decision(replay, "cost_model", load=lambda _v: None)
    assert result.status == ML_MODEL_UNAVAILABLE
    assert result.decision is None
    assert result.ml_model_version == "stub-v1"


def test_another_version_under_the_same_name_is_unavailable() -> None:
    ml, _, _, replay = _case(False)
    other = dataclasses.replace(
        ml,
        predictor=dataclasses.replace(ml.predictor, version="stub-v9"),  # type: ignore[type-var]
    )
    result = replay_decision(replay, "cost_model", load=lambda _v: other)
    assert result.status == ML_MODEL_UNAVAILABLE


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_a_fallback_decision_replays_without_its_model(split: bool) -> None:
    _, _, decision, replay = _case(split, raises=OSError("disk"))
    assert decision.ml == MlUse(version="stub-v1", error="OSError")
    assert replay["ml"] == {"version": "stub-v1", "fallback": "OSError"}
    result = replay_decision(replay, "cost_model", load=lambda _v: None)
    assert result.status == "ok" and result.decision is not None
    assert decision_view(result.decision) == decision_view(decision)


def test_a_fallback_replay_never_asks_a_loaded_model() -> None:
    ml, predictor, decision, replay = _case(False, raises=OSError("disk"))
    calls = predictor.calls
    result = replay_decision(replay, "cost_model", load=lambda _v: ml)
    assert predictor.calls == calls
    assert result.decision is not None
    assert decision_view(result.decision) == decision_view(decision)


def test_a_replay_without_ml_is_the_v014_replay() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=21)
    view = project(request, snapshot)
    decision = plan(request, view, "cost_model", now, calibration=_cal())
    replay = _stored(to_replay(request, view, now, calibration=_cal()))
    loaded: list[str] = []
    result = replay_decision(replay, "cost_model", load=lambda v: loaded.append(v))  # type: ignore[func-returns-value]
    assert loaded == [] and result.decision is not None
    assert decision_view(result.decision) == decision_view(decision)


@pytest.mark.parametrize(
    "block",
    [[], "v1", {"version": ""}, {"version": 3}, {"version": "v", "fallback": 1}],
)
def test_a_malformed_ml_key_is_a_replay_error(block: Any) -> None:
    request, snapshot, now = synthetic_fabric(3, split=False, seed=1)
    replay = to_replay(request, snapshot, now)
    replay["ml"] = block
    with pytest.raises(ReplayError):
        ml_from_replay(replay)


def test_the_training_side_reads_the_analytical_raw_back() -> None:
    _, _, decision, _ = _case(True)
    raw = plan(*_raw_inputs(), "cost_model", _raw_inputs_now())
    raw_rows = {
        r["node_id"]: r
        for r in candidates_json(raw)
        if r.get("node_set") and len(r["node_set"]) == 1
    }
    rows = [
        r
        for r in json.loads(json.dumps(candidates_json(decision)))
        if r.get("node_set") and len(r["node_set"]) == 1
    ]
    assert rows
    for row in rows:
        before = raw_rows[row["node_id"]]
        # the lane rate: corrected on the row, analytical in detail.ml.raw_s
        spi = _raw_seconds_per_item(row)
        assert spi == pytest.approx(1.0 / before["rate_items_s"], rel=1e-12)
        assert row["rate_items_s"] == pytest.approx(
            before["rate_items_s"] / math.exp(-0.3), rel=1e-5
        )
        env = row["estimate"]["terms"]["env"]
        if env["detail"]["ml"]["used"]:
            assert raw_prediction(env) == before["estimate"]["terms"]["env"]["seconds"]
            assert env["seconds"] != raw_prediction(env)


def _raw_inputs() -> tuple[Any, Any]:
    request, snapshot, _ = synthetic_fabric(4, split=True, seed=21)
    return request, project(request, snapshot)


def _raw_inputs_now() -> Any:
    return synthetic_fabric(4, split=True, seed=21)[2]


def test_an_ml_input_names_its_predictors_version() -> None:
    ml, _, _, _ = _case(False)
    assert isinstance(ml, MlInput) and ml.version == "stub-v1"
