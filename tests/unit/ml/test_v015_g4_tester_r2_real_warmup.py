"""V0.15 G4 tester round 2: warm-up with a REAL trained model (LightGBM / ridge
+ kNN OOD): after ``ml_input`` the very first live decision must be inside the
20 ms budget (no cold-call fallback) - for several freshly trained models and
both modes - and warm-up must not change any decision (same bytes as an
unwarmed predictor's decision under no budget)."""

from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows
from mf_planner_fabrics import synthetic_fabric
from mf_planner_golden import decision_view

from meshforge.controller.ml.predictor import BudgetedPredictor, ml_input
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.memory import InMemoryPredictor, train_in_memory
from meshforge.controller.planner import plan
from meshforge.controller.planner.predictor import ML_TERMS, MlInput
from meshforge.controller.planner.replay import project

pytestmark = pytest.mark.unit

SETTINGS = CompareSettings(terms=ML_TERMS, algorithms=("ridge", "lightgbm"))


def _train(seed: int) -> InMemoryPredictor:
    rows = synthetic_rows(
        "signal",
        families=5,
        groups_per_family=5,
        runs_per_group=8,
        terms=ML_TERMS,
        seed=seed,
    )
    return train_in_memory(table_from_rows(rows), SETTINGS)


def _case(n: int, split: bool) -> tuple[object, object, object]:
    request, snapshot, now = synthetic_fabric(n, split=split, seed=n + 3)
    snapshot = dataclasses.replace(
        snapshot, config=dataclasses.replace(snapshot.config, default_env_s=30.0)
    )
    request = dataclasses.replace(
        request,
        history=dataclasses.replace(
            request.history, output_bytes=5e8, output_samples=3
        ),
    )
    return request, project(request, snapshot), now  # type: ignore[arg-type]


@pytest.mark.parametrize("seed", [11, 12, 13])
@pytest.mark.parametrize("split", [False, True])
def test_the_first_live_decision_after_a_real_warm_up_does_not_fall_back(
    seed: int, split: bool
) -> None:
    fresh = _train(seed)
    assert fresh.models
    request, view, now = _case(4, split)
    ml = ml_input(fresh, budget_ms=20.0)
    assert isinstance(ml, MlInput)
    decision = plan(request, view, "cost_model", now, ml=ml)  # type: ignore[arg-type]
    assert decision.ml is not None
    assert decision.ml.error is None, (decision.ml.error, ml.predictor.last_ms)  # type: ignore[attr-defined]
    assert isinstance(ml.predictor, BudgetedPredictor)
    assert ml.predictor.last_ms is not None and ml.predictor.last_ms < 20.0


def test_warm_up_does_not_change_the_decision() -> None:
    request, view, now = _case(4, True)
    a = plan(request, view, "cost_model", now, ml=ml_input(_train(21), budget_ms=None))  # type: ignore[arg-type]
    cold = _train(21)
    b = plan(
        request,  # type: ignore[arg-type]
        view,  # type: ignore[arg-type]
        "cost_model",
        now,  # type: ignore[arg-type]
        ml=MlInput(cold, ml_input(_train(21), budget_ms=None).features),  # type: ignore[union-attr]
    )
    assert decision_view(a) == decision_view(b)


def test_a_real_model_with_no_learned_terms_warms_up_to_a_noop() -> None:
    empty = _train(5)
    empty.models.clear()
    ml = ml_input(empty)
    assert ml is not None
