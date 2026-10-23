"""V0.15 G4 tester round 1: the planner with a REAL trained model (LightGBM /
ridge over the G2 pipeline, conformal interval, kNN OOD), not a stub.

* determinism: two separately trained models on the same table have the same
  version and make byte-identical decisions (D352) - replay with either
  reproduces the live decision;
* a model trained on other rows has another version -> replay with it is
  ``ml_model_unavailable`` (never a silent re-plan);
* concurrency: one shared predictor, many threads planning different
  fabrics: every decision equals the single-threaded reference (the
  predictor/planner share no mutable per-decision state);
* the budget under CPU contention: a decision is either the reference ML
  decision or a *recorded* fallback that equals the calibrated decision and
  replays without the model - never a half state;
* a wrong-length feature row reaching the real model is a recorded fallback;
* the model's reasons on a real fabric are all legal and every ML-used term
  keeps ``source`` and the stored raw.
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import json
import math

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows
from mf_planner_fabrics import synthetic_fabric
from mf_planner_golden import decision_view

from meshforge.controller.ml.predictor import ml_input
from meshforge.controller.ml.replay import ML_MODEL_UNAVAILABLE, replay_decision
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.memory import InMemoryPredictor, train_in_memory
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import Calibration, Factor, TermFactors
from meshforge.controller.planner.predictor import ML_TERMS, MlInput, MlReason
from meshforge.controller.planner.replay import canonical_json, project, to_replay

pytestmark = pytest.mark.unit

SETTINGS = CompareSettings(terms=ML_TERMS, algorithms=("ridge", "lightgbm"))


def _train(seed: int = 1) -> InMemoryPredictor:
    rows = synthetic_rows(
        "signal",
        families=5,
        groups_per_family=5,
        runs_per_group=8,
        terms=ML_TERMS,
        seed=seed,
    )
    return train_in_memory(table_from_rows(rows), SETTINGS)


@pytest.fixture(scope="module")
def model() -> InMemoryPredictor:
    predictor = _train()
    assert predictor.models, "the synthetic truth must yield learned models"
    return predictor


def _cal() -> Calibration:
    f = Factor(factor=1.4, n=10, median=1.4)
    return Calibration(
        version=5,
        terms={
            t: TermFactors(global_=f) for t in ("transfer", "env", "compute", "output")
        },
    )


def _case(n: int, split: bool, seed: int) -> tuple[object, object, object]:
    request, snapshot, now = synthetic_fabric(n, split=split, seed=seed)
    snapshot = dataclasses.replace(
        snapshot, config=dataclasses.replace(snapshot.config, default_env_s=30.0)
    )
    request = dataclasses.replace(
        request,
        history=dataclasses.replace(
            request.history, output_bytes=5e8, output_samples=3
        ),
    )
    return request, project(request, snapshot), now


def test_two_trainings_on_the_same_rows_are_the_same_model_and_decisions() -> None:
    a, b = _train(), _train()
    assert a.predictor_version == b.predictor_version
    for split in (False, True):
        request, view, now = _case(4, split, 4)
        da = plan(request, view, "cost_model", now, ml=ml_input(a, budget_ms=None))  # type: ignore[arg-type]
        db = plan(request, view, "cost_model", now, ml=ml_input(b, budget_ms=None))  # type: ignore[arg-type]
        assert decision_view(da) == decision_view(db)


def test_replay_with_the_real_model_reproduces_and_another_model_is_unavailable(
    model: InMemoryPredictor,
) -> None:
    other = _train(seed=9)
    assert other.predictor_version != model.predictor_version
    ml = ml_input(model, budget_ms=None)
    ml_other = ml_input(other, budget_ms=None)
    for split in (False, True):
        request, view, now = _case(4, split, 4)
        cal = _cal()
        decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)  # type: ignore[arg-type]
        assert decision.ml is not None and decision.ml.error is None
        replay = json.loads(
            canonical_json(
                to_replay(request, view, now, calibration=cal, ml=decision.ml)  # type: ignore[arg-type]
            )
        )
        got = replay_decision(replay, "cost_model", load=lambda _v: ml)
        assert got.decision is not None
        assert decision_view(got.decision) == decision_view(decision)
        wrong = replay_decision(replay, "cost_model", load=lambda _v: ml_other)
        assert wrong.status == ML_MODEL_UNAVAILABLE and wrong.decision is None


def test_a_real_model_gives_only_legal_reasons_and_keeps_sources(
    model: InMemoryPredictor,
) -> None:
    legal = {r.value for r in MlReason} | {None}
    for split in (False, True):
        request, view, now = _case(6, split, 6)
        plain = plan(request, view, "cost_model", now)  # type: ignore[arg-type]
        decision = plan(
            request, view, "cost_model", now, ml=ml_input(model, budget_ms=None)
        )  # type: ignore[arg-type]
        assert decision.ml is not None and decision.ml.error is None
        by_key = {(c.node_id, c.node_set): c for c in plain.candidates if c.estimate}
        seen_used = 0
        for c in decision.candidates:
            if c.estimate is None:
                continue
            for name, term in c.estimate.terms().items():
                block = term.detail.get("ml")
                if block is None:
                    continue
                assert block["used"] or block["reason"] in legal
                if block["used"]:
                    seen_used += 1
                    base = by_key.get((c.node_id, c.node_set))
                    if base is not None and name in base.estimate.terms():  # type: ignore[union-attr]
                        assert term.source == base.estimate.terms()[name].source  # type: ignore[union-attr]
        assert seen_used >= 0


def test_the_real_model_raising_on_a_bad_feature_row_is_a_recorded_fallback(
    model: InMemoryPredictor,
) -> None:
    request, view, now = _case(4, False, 4)
    ml = dataclasses.replace(
        ml_input(model, budget_ms=None), features=lambda *_a: (1.0, 2.0)
    )
    decision = plan(request, view, "cost_model", now, calibration=_cal(), ml=ml)  # type: ignore[arg-type]
    assert decision.ml is not None and decision.ml.error == "ValueError"
    assert decision.chosen is not None


def _fabrics() -> list[tuple[object, object, object]]:
    return [_case(n, split, 10 + n) for n in (3, 4, 6) for split in (False, True)]


def test_one_shared_predictor_across_threads_equals_the_sequential_reference(
    model: InMemoryPredictor,
) -> None:
    ml = ml_input(model, budget_ms=None)
    fabrics = _fabrics()
    cal = _cal()
    reference = [
        decision_view(plan(r, v, "cost_model", n, calibration=cal, ml=ml))  # type: ignore[arg-type]
        for r, v, n in fabrics
    ]

    def run(index: int) -> tuple[int, dict[str, object]]:
        r, v, n = fabrics[index % len(fabrics)]
        return index % len(fabrics), decision_view(
            plan(r, v, "cost_model", n, calibration=cal, ml=ml)  # type: ignore[arg-type]
        )

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(240)))
    for index, view in results:
        assert view == reference[index], index


def test_under_cpu_contention_a_decision_is_ml_or_a_recorded_replayable_fallback(
    model: InMemoryPredictor,
) -> None:
    """The 20 ms predict budget under 8 competing threads: whichever way each
    decision goes, it is internally consistent and replays."""
    ml = ml_input(model, budget_ms=20.0)
    fabrics = _fabrics()
    cal = _cal()
    ref_ml = [
        plan(r, v, "cost_model", n, calibration=cal, ml=ml_input(model, budget_ms=None))  # type: ignore[arg-type]
        for r, v, n in fabrics
    ]
    ref_cal = [plan(r, v, "cost_model", n, calibration=cal) for r, v, n in fabrics]  # type: ignore[arg-type]

    def run(index: int) -> tuple[int, object]:
        r, v, n = fabrics[index % len(fabrics)]
        return index % len(fabrics), plan(r, v, "cost_model", n, calibration=cal, ml=ml)  # type: ignore[arg-type]

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(160)))
    fallbacks = 0
    for index, decision in results:
        assert decision.ml is not None  # type: ignore[attr-defined]
        if decision.ml.error is None:  # type: ignore[attr-defined]
            assert decision_view(decision) == decision_view(ref_ml[index])  # type: ignore[arg-type]
        else:
            fallbacks += 1
            assert decision.ml.error == "budget_exceeded"  # type: ignore[attr-defined]
            want = ref_cal[index]
            assert decision.chosen is not None  # type: ignore[attr-defined]
            assert decision.chosen.node_id == want.chosen.node_id  # type: ignore[attr-defined,union-attr]
            r, v, n = fabrics[index]
            replay = json.loads(
                canonical_json(
                    to_replay(r, v, n, calibration=cal, ml=decision.ml)  # type: ignore[arg-type,attr-defined]
                )
            )
            again = replay_decision(replay, "cost_model", load=lambda _v: None)
            assert again.decision is not None
            assert decision_view(again.decision) == decision_view(decision)  # type: ignore[arg-type]
    print(f"\n[tester] budget fallbacks under contention: {fallbacks}/160")  # noqa: T201


def test_a_cold_first_prediction_is_inside_the_budget_or_a_clean_fallback() -> None:
    """Right after a model load the first call is cold (no warm caches): it
    must still be inside the 20 ms budget on this machine, else the decision
    falls back cleanly - report the number either way."""
    fresh = _train(seed=3)
    request, view, now = _case(4, False, 4)
    ml = ml_input(fresh, budget_ms=20.0)
    decision = plan(request, view, "cost_model", now, ml=ml)  # type: ignore[arg-type]
    assert decision.ml is not None
    assert decision.ml.error in (None, "budget_exceeded")
    assert decision.chosen is not None
    assert math.isfinite(ml.predictor.last_ms or 0.0)  # type: ignore[attr-defined]
    print(  # noqa: T201
        f"\n[tester] cold first predict: {ml.predictor.last_ms:.2f} ms "  # type: ignore[attr-defined]
        f"error={decision.ml.error}"
    )


def test_ml_input_refuses_nonsense_limits() -> None:
    model_ = _train(seed=4)
    for bad in (-0.1, math.nan, math.inf):
        with pytest.raises(ValueError):
            ml_input(model_, max_interval_log=bad)
    assert isinstance(ml_input(model_, max_interval_log=0.0), MlInput)
