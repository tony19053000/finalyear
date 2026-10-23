"""V0.15 G4 (D336, D363, D364, Q21): the dispatcher plans with the tick's ML
model, names it on the decision row and in the replay.

* no model (``ml = None``, ``ml = "off"``) -> rows and replay exactly as in
  V0.14: no ``ml_model_version``, no ``ml`` replay key;
* a model -> every candidate row has ``ml_model_version``, the replay
  ``ml: {version}``, and ``replay_decision`` with that model gives the record
  again (candidates, digest, reason key, chosen node); the model really moves
  the dispatch; the campaign shadow plan uses it too;
* D363: a failing model never fails or delays a dispatch - the decision is
  the calibrated one, the row says ``ml_fallback``, the failure is counted on
  every decision and logged at most once a minute;
* :class:`StaticModelSource`, ``ml_input`` and the two new settings.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pytest
import structlog
from mf_ml_stub import Rule, StubPredictor, stub_ml
from pydantic import ValidationError
from test_v014_g2_dispatch_calibration import (
    A,
    B,
    _calibration,
    _fabric,
    _job,
    _planner,
)

from meshforge.controller.jobs.execution_store import Dispatch
from meshforge.controller.jobs.planning import ML_ERROR_LOG_EVERY_S
from meshforge.controller.ml.predictor import (
    DEFAULT_PREDICT_BUDGET_MS,
    BudgetedPredictor,
    StaticModelSource,
    ml_input,
)
from meshforge.controller.ml.replay import replay_decision
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import canonical_json, ml_from_replay
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit


def _record(decision: Any) -> Any:
    assert isinstance(decision, Dispatch) and decision.record is not None
    return decision.record


def test_without_a_model_rows_and_replay_are_the_v014_ones() -> None:
    planner = _planner()
    assert planner.ml is None
    record = _record(planner.decide(_job(), _fabric()))
    assert all("ml_model_version" not in r for r in record.candidates)
    assert "ml" not in (record.replay or {})


def test_a_model_is_named_on_every_row_and_in_the_replay() -> None:
    planner = _planner()
    planner.calibration = _calibration(5)
    ml, _ = stub_ml({("compute", None): Rule(y_hat=0.0)})
    planner.ml = ml
    record = _record(planner.decide(_job(), _fabric()))
    assert {r["ml_model_version"] for r in record.candidates} == {"stub-v1"}
    stored = json.loads(canonical_json(record.replay))
    assert ml_from_replay(stored) is not None and stored["ml"] == {"version": "stub-v1"}
    again = replay_decision(stored, record.policy, load=lambda v: ml)
    assert again.decision is not None
    assert candidates_json(again.decision) == list(record.candidates)
    assert again.decision.snapshot_digest == record.snapshot_digest
    assert again.decision.reason_key == record.reason_key


def test_the_model_moves_the_dispatch() -> None:
    planner = _planner()
    assert planner.decide(_job(), _fabric()).node_id == A  # type: ignore[union-attr]
    ml, _ = stub_ml(
        {
            ("compute", None): Rule(y_hat=0.0),
            ("compute", B): Rule(y_hat=math.log(0.5)),  # B twice as fast
        }
    )
    planner.ml = ml
    assert planner.decide(_job(), _fabric()).node_id == B  # type: ignore[union-attr]


def test_the_campaign_shadow_plan_uses_the_model() -> None:
    planner = _planner()
    ml, _ = stub_ml(
        {("compute", None): Rule(y_hat=0.0), ("compute", B): Rule(y_hat=math.log(0.5))}
    )
    planner.ml = ml
    record = _record(planner.decide(_job(A, bench=True), _fabric()))
    assert record.shadow_chosen_node_id == B


def test_a_failing_model_never_fails_the_dispatch_and_is_logged_once_a_minute() -> None:
    now = [1000.0]
    planner = _planner()
    planner._clock = lambda: now[0]  # type: ignore[method-assign]
    planner.calibration = _calibration(5)
    ml, stub = stub_ml({("compute", None): Rule()}, raises=MemoryError())
    planner.ml = ml
    calibrated = _planner()
    calibrated.calibration = _calibration(5)
    want = _record(calibrated.decide(_job(), _fabric()))
    with structlog.testing.capture_logs() as logs:
        for step in (0.0, 10.0, 59.0, 61.0):
            now[0] = 1000.0 + step
            record = _record(planner.decide(_job(), _fabric()))
            assert record.chosen_node_id == want.chosen_node_id
            assert all(r["ml_fallback"] == "predictor_error" for r in record.candidates)
            assert json.loads(canonical_json(record.replay))["ml"] == {
                "version": "stub-v1",
                "fallback": "MemoryError",
            }
    assert planner.ml_predictor_errors == 4
    assert stub.calls == 4
    events = [e for e in logs if e["event"] == "controller.jobs.ml_predictor_error"]
    assert len(events) == 2  # t=0 and t=61 (> ML_ERROR_LOG_EVERY_S)
    assert ML_ERROR_LOG_EVERY_S == 60.0
    assert events[0]["error_type"] == "MemoryError"
    assert events[0]["ml_model_version"] == "stub-v1"
    assert planner.planning_errors == 0


def test_static_model_source() -> None:
    ml, _ = stub_ml({})
    assert asyncio.run(StaticModelSource(ml).current()) is ml
    assert asyncio.run(StaticModelSource(None).current()) is None


def test_ml_input_wraps_the_predictor_in_its_budget() -> None:
    stub = StubPredictor(rules={("compute", None): Rule()})
    wrapped = ml_input(stub)
    assert isinstance(wrapped.predictor, BudgetedPredictor)
    assert wrapped.predictor.budget_ms == DEFAULT_PREDICT_BUDGET_MS == 20.0
    assert wrapped.version == "stub-v1" and wrapped.predictor.has_model("compute")
    assert ml_input(stub, budget_ms=None).predictor is stub
    with pytest.raises(ValueError, match="ml_max_interval_log"):
        ml_input(stub, max_interval_log=math.nan)


def test_the_ml_settings() -> None:
    s = PlannerSettings()
    assert s.ml_max_interval_log == pytest.approx(math.log(2.0))
    assert s.ml_predict_budget_ms == 20.0
    for bad in (
        {"ml_max_interval_log": -0.1},
        {"ml_predict_budget_ms": 0.0},
        {"ml_predict_budget_ms": -5.0},
    ):
        with pytest.raises(ValidationError):
            PlannerSettings(**bad)


# ------------------------------------------------ G4 fix r1 (G4-R1-2): warm-up


def test_ml_input_warms_the_model_up_outside_the_budget() -> None:
    stub = StubPredictor(rules={("compute", None): Rule(), ("env", None): Rule()})
    ml = ml_input(stub, budget_ms=20.0, clock=lambda: 0.0)
    assert ml is not None
    assert stub.calls == 1  # one warm-up batch ...
    assert {q.term for q in stub.queries} == {"compute", "env"}  # ... per term
    assert len(stub.queries) == 8
    assert ml.predictor.last_ms is None  # type: ignore[attr-defined]  # not budgeted
    # nothing to warm without a model: no call at all
    empty = StubPredictor(rules={})
    assert ml_input(empty) is not None and empty.calls == 0


@pytest.mark.parametrize("broken", ["raises", "short", "version"])
def test_a_model_failing_its_warm_up_is_unavailable_never_a_crash(broken: str) -> None:
    class Bad(StubPredictor):
        @property
        def predictor_version(self) -> str:
            if broken == "version":
                raise RuntimeError("no version")
            return "bad-v1"

    kwargs: dict[str, Any] = {}
    if broken == "raises":
        kwargs["raises"] = OSError("model file gone")
    if broken == "short":
        kwargs["answer_count_delta"] = -1
    stub = Bad(rules={("compute", None): Rule()}, **kwargs)
    with structlog.testing.capture_logs() as logs:
        assert ml_input(stub) is None
    (event,) = [e for e in logs if e["event"] == "controller.ml.model_warm_up_failed"]
    assert event["error_type"] in {"OSError", "malformed_prediction", "RuntimeError"}
