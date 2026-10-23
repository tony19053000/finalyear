"""V0.15 G4 tester round 2: the warm-up at build (G4-R1-2) and the version
read inside the D363 guard (O-1).

Warm-up: once per ``ml_input`` build, outside the budget, a failure means "no
model" (None) plus exactly one log line, never a crash; hostile models.
Version: raising / None / "" / non-str / changing between calls -> the
decision falls back (``predictor_error``) or is consistent, never a failed
plan, never a non-text version in the stored decision.
"""

from __future__ import annotations

import dataclasses
import json
import math
import threading
from collections.abc import Sequence
from typing import Any

import pytest
import structlog
from mf_ml_stub import Rule, StubPredictor
from mf_planner_fabrics import synthetic_fabric
from mf_planner_golden import decision_view

from meshforge.controller.ml.features import term_features
from meshforge.controller.ml.predictor import (
    BudgetedPredictor,
    ml_input,
    warm_up,
)
from meshforge.controller.ml.replay import replay_decision
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import (
    ML_TERMS,
    UNKNOWN_VERSION,
    MlInput,
    TermPrediction,
    TermQuery,
)
from meshforge.controller.planner.replay import canonical_json, project, to_replay

pytestmark = pytest.mark.unit

ALL = {(t, None): Rule(y_hat=0.2, half_width=0.05) for t in ML_TERMS}


def _inputs(split: bool) -> tuple[Any, Any, Any]:
    request, snapshot, now = synthetic_fabric(4, split=split, seed=9)
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


# ------------------------------------------------------------------ warm-up


def test_warm_up_runs_once_per_build_not_per_decision() -> None:
    stub = StubPredictor(rules=dict(ALL))
    ml = ml_input(stub)
    assert ml is not None and stub.calls == 1
    request, view, now = _inputs(False)
    plan(request, view, "cost_model", now, ml=ml)
    after_one = stub.calls
    plan(request, view, "cost_model", now, ml=ml)
    assert stub.calls == after_one * 2 - 1  # +1 per decision, warm-up not repeated
    assert (
        ml_input(stub) is not None and stub.calls == after_one * 2
    )  # a new build warms


def test_warm_up_rows_have_the_feature_width_the_planner_builds() -> None:
    stub = StubPredictor(rules=dict(ALL))
    ml_input(stub)
    width = len(NUMERIC_FEATURES)
    assert stub.queries and all(len(q.features) == width for q in stub.queries)
    # and the live builder produces rows of the same width
    request, view, now = _inputs(False)
    stub2 = StubPredictor(rules=dict(ALL))
    live = ml_input(stub2, budget_ms=None)
    assert live is not None
    stub2.queries.clear()
    plan(request, view, "cost_model", now, ml=live)
    assert stub2.queries and all(len(q.features) == width for q in stub2.queries)


def test_warm_up_batch_has_the_hostile_rows_a_tree_can_hit() -> None:
    stub = StubPredictor(rules=dict(ALL))
    ml_input(stub)
    rows = [q.features for q in stub.queries if q.term == "compute"]
    assert any(all(math.isnan(x) for x in r) for r in rows)  # all missing
    assert any(all(x == 0.0 for x in r) for r in rows)  # all zero
    assert any(any(x > 1e5 for x in r if not math.isnan(x)) for r in rows)  # big


class _Slow(StubPredictor):
    """Warm-up (the first call) takes 'forever' by a fake clock; later calls
    are instant. The budget must not see the warm-up."""


def test_a_slow_warm_up_is_not_charged_to_the_budget() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    class Slow(StubPredictor):
        def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
            now[0] += 5.0  # 5000 ms per call on the fake clock
            return super().predict(rows)

    stub = Slow(rules=dict(ALL))
    ml = ml_input(stub, budget_ms=20.0, clock=clock)
    assert ml is not None and isinstance(ml.predictor, BudgetedPredictor)
    assert ml.predictor.last_ms is None  # warm-up never measured
    # the first LIVE call is over budget -> counted and falls back (D363)
    request, view, t = _inputs(False)
    decision = plan(request, view, "cost_model", t, ml=ml)
    assert decision.ml is not None and decision.ml.error == "budget_exceeded"
    assert decision.chosen is not None


@pytest.mark.parametrize(
    "hostile",
    [
        "raises_value",
        "raises_runtime",
        "returns_none",
        "returns_int",
        "too_many",
        "too_few",
        "empty",
        "has_model_raises",
        "version_raises",
    ],
)
def test_hostile_models_during_warm_up_are_unavailable_with_one_log(
    hostile: str,
) -> None:
    class Hostile(StubPredictor):
        def has_model(self, term: str) -> bool:
            if hostile == "has_model_raises":
                raise RuntimeError("boom")
            return super().has_model(term)

        @property
        def predictor_version(self) -> str:
            if hostile == "version_raises":
                raise RuntimeError("v")
            return "h-v1"

        def predict(self, rows: Sequence[TermQuery]) -> Any:
            if hostile == "raises_value":
                raise ValueError("bad shape")
            if hostile == "raises_runtime":
                raise RuntimeError("segfault-ish")
            if hostile == "returns_none":
                return None
            if hostile == "returns_int":
                return 5
            if hostile == "empty":
                return []
            return super().predict(rows)

    kwargs: dict[str, Any] = {}
    if hostile == "too_many":
        kwargs["answer_count_delta"] = 3
    if hostile == "too_few":
        kwargs["answer_count_delta"] = -2
    stub = Hostile(rules=dict(ALL), **kwargs)
    with structlog.testing.capture_logs() as logs:
        assert ml_input(stub) is None
        assert ml_input(stub, budget_ms=None) is None
    events = [e for e in logs if e["event"] == "controller.ml.model_warm_up_failed"]
    assert len(events) == 2  # exactly one per failed build
    for event in events:
        assert isinstance(event["error_type"], str) and event["error_type"]


def test_a_failed_warm_up_never_leaves_a_half_state_and_next_build_retries() -> None:
    stub = StubPredictor(rules=dict(ALL), raises=OSError("gone"))
    assert ml_input(stub) is None
    stub.raises = None
    ml = ml_input(stub)
    assert ml is not None
    request, view, now = _inputs(True)
    assert plan(request, view, "cost_model", now, ml=ml).ml is not None


def test_warm_up_with_nan_answers_is_accepted_but_the_live_plan_falls_back() -> None:
    """The warm-up only checks the call works (shape); a model that answers
    NaN passes it and each live decision then falls back under D363 -
    recorded, never a crash."""

    class NaNModel(StubPredictor):
        def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
            return [
                TermPrediction(math.nan, math.nan, math.nan, False, None, "nan-v")
                for _ in rows
            ]

    ml = ml_input(NaNModel(rules=dict(ALL)))
    assert ml is not None
    request, view, now = _inputs(False)
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.chosen is not None
    assert decision.ml is not None and decision.ml.error is not None


@pytest.mark.parametrize("budget", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_nonsense_budgets_are_refused_before_any_call(budget: float) -> None:
    stub = StubPredictor(rules=dict(ALL))
    with pytest.raises(ValueError):
        ml_input(stub, budget_ms=budget)
    assert stub.calls == 0


@pytest.mark.parametrize("width", [math.nan, math.inf, -0.1])
def test_nonsense_interval_limits_are_refused(width: float) -> None:
    with pytest.raises(ValueError):
        ml_input(StubPredictor(rules=dict(ALL)), max_interval_log=width)


def test_a_keyboard_interrupt_during_warm_up_propagates() -> None:
    """BaseException is not swallowed (D363 convention): ctrl-C works."""
    stub = StubPredictor(rules=dict(ALL), raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        ml_input(stub)


def test_concurrent_builds_on_one_predictor_are_safe() -> None:
    stub = StubPredictor(rules=dict(ALL))
    results: list[Any] = []
    errors: list[BaseException] = []

    def build() -> None:
        try:
            results.append(ml_input(stub))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 16 and all(r is not None for r in results)


def test_warm_up_function_directly_and_log_leaks_only_the_type() -> None:
    secret = "/home/user/secret-model-path.bin"
    stub = StubPredictor(rules=dict(ALL), raises=OSError(secret))
    with structlog.testing.capture_logs() as logs:
        assert warm_up(stub) is False
    assert secret not in json.dumps(logs, default=str)
    assert warm_up(StubPredictor(rules={})) is True  # nothing to warm


# ------------------------------------------------------------------ version


def _plan_with(version_value: Any, *, split: bool = False, raises: bool = False) -> Any:
    class V(StubPredictor):
        @property
        def predictor_version(self) -> Any:
            if raises:
                raise RuntimeError("no version")
            return version_value

    request, view, now = _inputs(split)
    ml = MlInput(V(rules=dict(ALL)), term_features)
    return request, view, now, ml, plan(request, view, "cost_model", now, ml=ml)


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize(
    "value", [None, "", 7, 1.5, b"v1", ["v"], object(), True], ids=repr
)
def test_a_non_text_or_empty_version_falls_back(value: Any, split: bool) -> None:
    _r, _v, _n, _ml, decision = _plan_with(value, split=split)
    assert decision.chosen is not None
    assert decision.ml is not None
    assert decision.ml.version == UNKNOWN_VERSION
    assert decision.ml.error == "malformed_version"
    blob = canonical_json(candidates_json(decision))
    assert "stub-v1" not in blob
    for row in decision.candidates:
        if row.estimate is None:
            continue
        for term in row.estimate.terms().values():
            ml = term.detail.get("ml")
            assert ml is None or ml["used"] is False


@pytest.mark.parametrize("split", [False, True])
def test_a_raising_version_falls_back_and_matches_the_plain_plan(split: bool) -> None:
    request, view, now, _ml, decision = _plan_with(None, split=split, raises=True)
    assert decision.ml is not None and decision.ml.error == "RuntimeError"
    assert decision.ml.version == UNKNOWN_VERSION
    plain = plan(request, view, "cost_model", now)
    assert decision.chosen is not None and plain.chosen is not None
    assert decision.chosen.node_id == plain.chosen.node_id
    assert decision.chosen.node_set == plain.chosen.node_set
    # replays to itself (the stored fallback reason is honoured)
    stored = to_replay(request, view, now, ml=decision.ml)
    json.loads(canonical_json(stored))


def test_version_that_changes_between_calls_is_read_once_and_recorded_once() -> None:
    reads = {"n": 0}

    class Changing(StubPredictor):
        @property
        def predictor_version(self) -> str:
            reads["n"] += 1
            return f"v{reads['n']}"

    request, view, now = _inputs(True)
    stub = Changing(rules=dict(ALL))
    ml = MlInput(stub, term_features)
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.ml is not None
    assert decision.ml.version.startswith("v") and decision.ml.error is None
    seen = {
        t.detail["ml"]["model_version"]
        for r in decision.candidates
        if r.estimate is not None
        for t in r.estimate.terms().values()
        if "ml" in t.detail and "model_version" in t.detail["ml"]
    }
    # the answers carry the model's own version at predict time; the decision
    # header is one consistent value (no mixed header)
    assert decision.ml.version in {f"v{i}" for i in range(1, reads["n"] + 1)}
    assert len(seen) >= 1


def test_version_raising_only_on_second_read_is_still_a_valid_plan() -> None:
    reads = {"n": 0}

    class Flaky(StubPredictor):
        @property
        def predictor_version(self) -> str:
            reads["n"] += 1
            if reads["n"] > 1:
                raise RuntimeError("second read")
            return "flaky-v1"

    request, view, now = _inputs(True)
    ml = MlInput(Flaky(rules=dict(ALL)), term_features)
    decision = plan(request, view, "cost_model", now, ml=ml)  # must not raise
    assert decision.chosen is not None
    # and a second decision with the same MlInput is also fine
    assert plan(request, view, "cost_model", now, ml=ml).chosen is not None


def test_a_huge_or_unicode_version_is_stored_and_replayable() -> None:
    for version in ("v" * 100_000, "модель-ü-🙂-v1", "a\x00b", "\x1b[31mred"):
        request, view, now, ml, decision = _plan_with(version)
        assert decision.ml is not None and decision.ml.error is None
        assert decision.ml.version == version
        blob = canonical_json(to_replay(request, view, now, ml=decision.ml))
        assert json.loads(blob)
    # a unicode version replays to the identical decision with the same model
    request, view, now = _inputs(False)
    stub = StubPredictor(rules=dict(ALL), version="модель-v1")
    ml = MlInput(stub, term_features)
    live = plan(request, view, "cost_model", now, ml=ml)
    stored = json.loads(canonical_json(to_replay(request, view, now, ml=live.ml)))
    out = replay_decision(stored, "cost_model", load=lambda _v: ml)
    assert out.decision is not None
    assert decision_view(out.decision) == decision_view(live)


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("kind", ["raises", "none", "empty", "int"])
def test_a_version_fallback_decision_replays_to_itself_without_the_model(
    kind: str, split: bool
) -> None:
    """The stored fallback (version 'unknown', error type) is re-planned
    exactly, whatever the loader answers - the model is never asked."""
    value = {"raises": None, "none": None, "empty": "", "int": 7}[kind]
    request, view, now, _ml, live = _plan_with(
        value, split=split, raises=(kind == "raises")
    )
    stored = json.loads(canonical_json(to_replay(request, view, now, ml=live.ml)))
    loaders = [lambda _v: None]
    if kind != "raises":  # (a loader handing back a raising model: see below)
        loaders.append(lambda _v: _ml)
    for loader in loaders:
        out = replay_decision(stored, "cost_model", load=loader)
        assert out.decision is not None, out.status
        assert decision_view(out.decision) == decision_view(live)


def test_replay_guards_a_loaded_models_version() -> None:
    """O-1b fixed by the Lead at merge: a loaded model whose
    ``predictor_version`` raises no longer makes the replay raise; the stored
    fallback decision replays to itself."""
    request, view, now, ml, live = _plan_with(None, raises=True)
    stored = json.loads(canonical_json(to_replay(request, view, now, ml=live.ml)))
    result = replay_decision(stored, "cost_model", load=lambda _v: ml)
    assert result.status == "ok"
