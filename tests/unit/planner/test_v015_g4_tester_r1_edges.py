"""V0.15 G4 tester round 1: edges and failure injection of the ML planner.

* absurd but legal predictions (|y_hat| = 50 on every term, Mode 1 and
  Mode 3): the plan is finite, serialisable and replays to itself;
* every kind of malformed answer (wrong type, None rows, numpy scalars,
  generators, bool, str) -> a decision, never an exception (D363);
* ``BaseException`` (KeyboardInterrupt, CancelledError) is NOT swallowed;
* ``has_model`` raising, the feature builder returning garbage;
* raw-term floor boundary (0.05 s inclusive) through ``MlSession`` directly;
* Mode 3: the margin test, the makespan and the chosen set are consistent
  with the *ML-corrected* totals; an ML combine blow-up flips set -> single;
  a mixed OOD / in-distribution lane keeps its raw rate;
* a predictor returning the same answers in a different order is not
  accepted silently (positions are the contract);
* the Decision / candidates JSON stays finite for every combination.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
from collections.abc import Sequence
from typing import Any

import pytest
from mf_ml_stub import Rule, StubPredictor, stub_ml
from mf_planner_fabrics import synthetic_fabric
from mf_planner_golden import decision_view

from meshforge.controller.ml.features import term_features
from meshforge.controller.ml.replay import replay_decision
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import (
    ML_TERMS,
    MlInput,
    MlReason,
    MlSession,
    PredictorFailed,
    TermContext,
    TermPrediction,
    TermQuery,
)
from meshforge.controller.planner.replay import canonical_json, project, to_replay
from meshforge.controller.planner.types import Decision

pytestmark = pytest.mark.unit


def _inputs(split: bool, n: int = 4, forced: bool = False) -> tuple[Any, Any, Any]:
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
    if forced:
        request = dataclasses.replace(
            request, node_set=tuple(x.node_id for x in snapshot.nodes)
        )
    return request, project(request, snapshot), now


def _finite_decision(decision: Decision) -> None:
    for c in decision.candidates:
        if c.estimate is not None:
            assert math.isfinite(c.estimate.total_s), c
            for term in c.estimate.terms().values():
                assert math.isfinite(term.seconds)
        if c.score is not None:
            assert math.isfinite(c.score)
    if decision.split is not None:
        assert math.isfinite(decision.split.predicted_makespan_s)
        for lane in decision.split.lanes:
            assert math.isfinite(lane.rate_items_s) and lane.rate_items_s >= 0.0
            assert math.isfinite(lane.start_s)
    blob = canonical_json(candidates_json(decision))
    assert "NaN" not in blob and "Infinity" not in blob


# ------------------------------------------------------------ absurd values


@pytest.mark.parametrize("y", [-50.0, 50.0, -49.999, 49.999])
@pytest.mark.parametrize("term", ML_TERMS)
@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_absurd_but_legal_corrections_stay_finite_and_replayable(
    y: float, term: str, split: bool
) -> None:
    request, view, now = _inputs(split)
    ml, _ = stub_ml({(term, None): Rule(y_hat=y, half_width=0.1)})
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.ml is not None and decision.ml.error is None
    _finite_decision(decision)
    assert decision.chosen is not None
    replay = json.loads(canonical_json(to_replay(request, view, now, ml=decision.ml)))
    again = replay_decision(replay, "cost_model", load=lambda _v: ml)
    assert again.decision is not None
    assert decision_view(again.decision) == decision_view(decision)


@pytest.mark.parametrize("y", [50.000001, -50.000001, 1e300, -1e300])
def test_beyond_the_limit_is_a_fallback_not_a_correction(y: float) -> None:
    request, view, now = _inputs(False)
    ml, _ = stub_ml({("compute", None): Rule(y_hat=y)})
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.ml is not None and decision.ml.error == "malformed_prediction"
    _finite_decision(decision)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("field", ["y_hat", "lo", "hi"])
def test_non_finite_anywhere_in_an_answer_is_a_fallback(field: str, bad: float) -> None:
    request, view, now = _inputs(False)

    class P(StubPredictor):
        def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
            good = super().predict(rows)
            first = dataclasses.replace(good[0], **{field: bad})
            return [first, *good[1:]]

    stub = P(rules={("compute", None): Rule(y_hat=0.1)})
    decision = plan(request, view, "cost_model", now, ml=MlInput(stub, term_features))
    assert decision.ml is not None and decision.ml.error == "malformed_prediction"


# ------------------------------------------------------------ malformed answers


class _Custom:
    """A predictor whose answer is whatever the test builds."""

    def __init__(self, build: Any, models: tuple[str, ...] = ("compute",)) -> None:
        self._build = build
        self._models = models
        self.calls = 0

    @property
    def predictor_version(self) -> str:
        return "custom-1"

    def has_model(self, term: str) -> bool:
        return term in self._models

    def predict(self, rows: Sequence[TermQuery]) -> Any:
        self.calls += 1
        return self._build(rows)


def _ok(y: float = 0.1) -> Any:
    return lambda rows: (
        [TermPrediction(y, y - 0.1, y + 0.1, False, None, "custom-1")] * len(rows)
    )


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("none", lambda rows: None),
        ("int", lambda rows: 3),
        ("dicts", lambda rows: [{"y_hat": 0.1}] * len(rows)),
        ("tuples", lambda rows: [(0.1, 0.0, 0.2, False, None, "v")] * len(rows)),
        ("nones", lambda rows: [None] * len(rows)),
        (
            "str_y",
            lambda rows: (
                [TermPrediction("0.1", 0.0, 0.2, False, None, "v")] * len(rows)
            ),  # type: ignore[arg-type]
        ),
        (
            "bool_y",
            lambda rows: [TermPrediction(True, 0.0, 2.0, False, None, "v")] * len(rows),  # type: ignore[arg-type]
        ),
        (
            "none_y",
            lambda rows: [TermPrediction(None, 0.0, 2.0, False, None, "v")] * len(rows),  # type: ignore[arg-type]
        ),
        (
            "bad_version",
            lambda rows: [TermPrediction(0.1, 0.0, 0.2, False, None, 7)] * len(rows),  # type: ignore[arg-type]
        ),
    ],
)
@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_a_malformed_answer_never_fails_the_plan(
    name: str, build: Any, split: bool
) -> None:
    request, view, now = _inputs(split)
    stub = _Custom(build, models=ML_TERMS)
    ml = MlInput(stub, term_features)  # type: ignore[arg-type]
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.ml is not None and decision.ml.error is not None, name
    assert decision.chosen is not None
    _finite_decision(decision)
    plain = plan(request, view, "cost_model", now)
    assert decision.chosen.node_id == plain.chosen.node_id  # type: ignore[union-attr]
    assert stub.calls == 1  # the fallback does not ask again


def test_a_generator_and_a_tuple_answer_are_accepted() -> None:
    request, view, now = _inputs(False)
    for wrap in (iter, tuple, list):
        stub = _Custom(lambda rows, w=wrap: w(_ok()(rows)))
        decision = plan(
            request, view, "cost_model", now, ml=MlInput(stub, term_features)
        )  # type: ignore[arg-type]
        assert decision.ml is not None and decision.ml.error is None, wrap


def test_numpy_scalars_are_accepted_or_fall_back_never_raise() -> None:
    np = pytest.importorskip("numpy")
    request, view, now = _inputs(False)
    for scalar in (np.float64, np.float32, np.int64):
        stub = _Custom(
            lambda rows, s=scalar: [
                TermPrediction(
                    s(0.1), s(0.0), s(0.2), np.bool_(False), None, "custom-1"
                )  # type: ignore[arg-type]
                for _ in rows
            ]
        )
        decision = plan(
            request, view, "cost_model", now, ml=MlInput(stub, term_features)
        )  # type: ignore[arg-type]
        assert decision.chosen is not None
        _finite_decision(decision)
        # either used, or a recorded fallback - never a half state
        blocks = [
            c.estimate.compute.detail["ml"]
            for c in decision.candidates
            if c.estimate is not None
        ]
        assert blocks
        if decision.ml is not None and decision.ml.error is None:
            assert all(b["used"] for b in blocks)


def test_numpy_bool_ood_flag_is_honoured() -> None:
    np = pytest.importorskip("numpy")
    request, view, now = _inputs(False)
    stub = _Custom(
        lambda rows: [
            TermPrediction(0.1, 0.0, 0.2, np.bool_(True), "knn", "custom-1")
            for _ in rows
        ]
    )
    decision = plan(request, view, "cost_model", now, ml=MlInput(stub, term_features))  # type: ignore[arg-type]
    assert decision.ml is not None and decision.ml.error is None
    for c in decision.candidates:
        if c.estimate is not None:
            assert c.estimate.compute.detail["ml"]["used"] is False


# ------------------------------------------------------------ BaseException


@pytest.mark.parametrize("exc", [KeyboardInterrupt, asyncio.CancelledError, SystemExit])
def test_base_exceptions_are_not_swallowed(exc: type[BaseException]) -> None:
    request, view, now = _inputs(False)
    stub, _ = stub_ml({("compute", None): Rule()}, raises=exc())
    with pytest.raises(exc):
        plan(request, view, "cost_model", now, ml=stub)


@pytest.mark.parametrize(
    "exc", [MemoryError, RecursionError, OSError, ValueError, KeyError]
)
def test_ordinary_failures_of_any_class_fall_back(exc: type[Exception]) -> None:
    request, view, now = _inputs(True)
    ml, _ = stub_ml({("chunk_rate", None): Rule()}, raises=exc("boom"))
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.ml is not None and decision.ml.error == exc.__name__
    assert decision.chosen is not None


def test_has_model_raising_falls_back() -> None:
    request, view, now = _inputs(False)

    class P(StubPredictor):
        def has_model(self, term: str) -> bool:
            raise RuntimeError("registry gone")

    decision = plan(
        request, view, "cost_model", now, ml=MlInput(P(rules={}), term_features)
    )
    assert decision.ml is not None and decision.ml.error == "RuntimeError"
    assert decision.chosen is not None


def test_a_raising_predictor_version_escapes_d363_today_observation() -> None:
    """OBSERVATION (hardening, not asserted as desired): ``MlInput.version`` is
    read when the session is created, outside the D363 fallback. A predictor
    whose ``predictor_version`` raises therefore makes ``plan()`` raise (the
    dispatcher then leaves every job queued while that model is active).
    Pinned here so a change is noticed; the state afterwards is clean."""
    request, view, now = _inputs(False)

    class P(StubPredictor):
        @property
        def predictor_version(self) -> str:
            raise RuntimeError("no version")

    with pytest.raises(RuntimeError):
        plan(request, view, "cost_model", now, ml=MlInput(P(), term_features))
    # and the next plain plan is unaffected
    assert plan(request, view, "cost_model", now).chosen is not None


def test_a_feature_row_of_any_garbage_reaches_the_predictor_only() -> None:
    request, view, now = _inputs(False)
    seen: list[Any] = []

    def feats(*_a: Any) -> tuple[float, ...]:
        return (math.nan, math.inf, -1.0)

    class P(StubPredictor):
        def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
            seen.extend(r.features for r in rows)
            return super().predict(rows)

    decision = plan(
        request,
        view,
        "cost_model",
        now,
        ml=MlInput(P(rules={("compute", None): Rule()}), feats),
    )
    assert seen and decision.ml is not None and decision.ml.error is None


# ------------------------------------------------------------ MlSession floor


def _session(stub: StubPredictor) -> MlSession:
    request, view, now = _inputs(False)
    return MlSession(MlInput(stub, term_features), request, view, now)


@pytest.mark.parametrize(
    ("raw", "asked"),
    [
        (0.05, True),
        (0.0499999999, False),
        (0.0, False),
        (-1.0, False),
        (math.nan, False),
        (math.inf, True),
        (1e9, True),
    ],
)
def test_the_dataset_floor_is_inclusive(raw: float, asked: bool) -> None:
    stub = StubPredictor(rules={("compute", None): Rule(y_hat=0.1)})
    out = _session(stub).resolve(
        [TermContext(term="compute", node_id="n", raw_s=raw, source="history")]
    )
    assert (stub.calls == 1) is asked
    assert out[0].used is asked
    if not asked:
        assert out[0].reason is MlReason.BELOW_MIN


def test_chunk_rate_floor_is_positive_only() -> None:
    stub = StubPredictor(rules={("chunk_rate", None): Rule()})
    s = _session(stub)
    for raw, asked in ((1e-12, True), (0.0, False), (-1.0, False), (math.nan, False)):
        stub.calls = 0
        (out,) = s.resolve(
            [TermContext(term="chunk_rate", node_id="n", raw_s=raw, source="history")]
        )
        assert (stub.calls == 1) is asked, raw
        assert out.used is asked


def test_precedence_order_of_reasons_is_fixed() -> None:
    """exact beats predictor_error beats no_model beats below_min beats ood
    beats too-wide, and an empty context list never asks."""
    stub = StubPredictor(rules={("compute", None): Rule(ood=True, half_width=9.0)})
    s = _session(stub)
    assert s.resolve([]) == []
    assert stub.calls == 0
    ctx = [
        TermContext("transfer", "n", 5.0, "assumed", {"missing_bytes": 0}),
        TermContext("env", "n", 5.0, "assumed", {"env_cached": True}),
        TermContext("output", "n", 5.0, "assumed", {}),  # no model
        TermContext("compute", "n", 0.01, "assumed", {}),  # below min
        TermContext("compute", "n", 5.0, "assumed", {}),  # ood (and too wide)
    ]
    out = s.resolve(ctx)
    assert [o.reason for o in out] == [
        MlReason.EXACT,
        MlReason.EXACT,
        MlReason.NO_MODEL,
        MlReason.BELOW_MIN,
        MlReason.OOD,
    ]
    failed = MlSession(
        MlInput(stub, term_features),
        *_inputs(False),
        failed="X",  # type: ignore[arg-type]
    )
    out = failed.resolve(ctx)
    assert [o.reason for o in out] == [
        MlReason.EXACT,
        MlReason.EXACT,
        MlReason.PREDICTOR_ERROR,
        MlReason.PREDICTOR_ERROR,
        MlReason.PREDICTOR_ERROR,
    ]


def test_exact_by_construction_ignores_lookalike_details() -> None:
    """Only a true zero count / a real True counts: ``missing_bytes`` of
    False, 0.0, "0", None and ``env_cached`` of 1 / "yes" are not exact."""
    stub = StubPredictor(rules={("transfer", None): Rule(), ("env", None): Rule()})
    s = _session(stub)
    contexts = [
        TermContext("transfer", f"t{i}", 5.0, "assumed", {"missing_bytes": v})
        for i, v in enumerate([False, 0.0, "0", None, 1])
    ] + [
        TermContext("env", f"e{i}", 5.0, "assumed", {"env_cached": v})
        for i, v in enumerate([1, "yes", None, False])
    ]
    out = s.resolve(contexts)
    assert all(o.reason is not MlReason.EXACT for o in out), [o.reason for o in out]
    assert stub.calls == 1 and len(stub.queries) == len(contexts)


def test_mismatched_answer_count_is_a_hard_failure_not_a_zip_truncation() -> None:
    stub = StubPredictor(rules={("compute", None): Rule()}, answer_count_delta=-1)
    s = _session(stub)
    with pytest.raises(PredictorFailed):
        s.resolve(
            [
                TermContext("compute", "a", 5.0, "assumed"),
                TermContext("compute", "b", 5.0, "assumed"),
            ]
        )


# ------------------------------------------------------------ Mode 3 coherence


def _split_decision(rules: dict[Any, Rule], forced: bool = True) -> Decision:
    request, view, now = _inputs(True, forced=forced)
    ml, _ = stub_ml(rules)
    return plan(request, view, "cost_model", now, ml=ml)


def test_margin_makespan_and_rows_agree_on_the_corrected_totals() -> None:
    request, view, now = _inputs(True)
    for rules in (
        {("chunk_rate", "node_001"): Rule(y_hat=math.log(0.2))},
        {("combine", None): Rule(y_hat=math.log(3.0))},
        {("lane_stage", None): Rule(y_hat=math.log(5.0))},
        {("lane_env", None): Rule(y_hat=math.log(0.1))},
    ):
        for term in ML_TERMS:
            rules.setdefault((term, None), Rule(y_hat=0.0))
        ml, _ = stub_ml(rules)
        decision = plan(request, view, "cost_model", now, ml=ml)
        assert decision.split is not None and decision.chosen is not None
        assert decision.chosen.estimate is not None
        assert decision.split.predicted_makespan_s == pytest.approx(
            decision.chosen.estimate.total_s, rel=1e-6
        )
        margin = decision.split.margin
        singles = {
            c.node_id: c
            for c in decision.candidates
            if c.feasible and c.node_set and len(c.node_set) == 1 and c.estimate
        }
        if margin.best_single_node_id is not None:
            single = singles[margin.best_single_node_id].estimate
            assert single is not None
            assert margin.best_single_s == pytest.approx(single.total_s, rel=1e-6)
            # no single is better than the one the margin test compared
            assert margin.best_single_s <= min(
                c.estimate.total_s for c in singles.values() if c.estimate
            ) * (1 + 1e-9)
        if margin.set_s is not None and margin.threshold_s is not None:
            assert margin.passed == (margin.set_s <= margin.threshold_s)


def test_an_ml_combine_blow_up_flips_set_to_single() -> None:
    zero = {(t, None): Rule(y_hat=0.0) for t in ML_TERMS}
    zero[("chunk_rate", None)] = Rule(y_hat=math.log(40.0))  # compute-bound
    before = _split_decision(zero, forced=False)
    assert before.split is not None and before.split.split, "need a split baseline"
    blow = {**zero, ("combine", None): Rule(y_hat=math.log(5000.0))}
    after = _split_decision(blow, forced=False)
    assert after.split is not None
    assert after.split.split is False
    assert after.split.margin.set_s is not None
    assert after.split.margin.set_s > after.split.margin.threshold_s  # type: ignore[operator]


def test_an_ml_stage_blow_up_on_one_node_removes_it_from_the_plan() -> None:
    zero = {(t, None): Rule(y_hat=0.0) for t in ML_TERMS}
    zero[("chunk_rate", None)] = Rule(y_hat=math.log(40.0))  # compute-bound
    before = _split_decision(zero, forced=False)
    assert before.split is not None and before.split.split
    # a lane that really stages bytes (the tree holders are exact: not asked)
    victim = next(x.node_id for x in before.split.lanes if x.node_id == "node_002")
    slow = {**zero, ("lane_stage", victim): Rule(y_hat=math.log(1e4))}
    after = _split_decision(slow, forced=False)
    assert after.split is not None
    first = next((x for x in after.split.lanes if x.node_id == victim), None)
    assert first is None or not first.active or first.start_s > 100.0


def test_a_mixed_ood_lane_keeps_its_raw_rate_while_the_rest_are_corrected() -> None:
    request, view, now = _inputs(True, forced=True)
    plain = candidates_json(plan(request, view, "cost_model", now))
    raw_rate = {
        r["node_id"]: r["rate_items_s"]
        for r in plain
        if r.get("node_set") and len(r["node_set"]) == 1 and r.get("feasible")
    }
    rules = {
        ("chunk_rate", None): Rule(y_hat=math.log(0.5)),
        ("chunk_rate", "node_002"): Rule(y_hat=math.log(0.01), ood=True),
    }
    ml, _ = stub_ml(rules)
    rows = candidates_json(plan(request, view, "cost_model", now, ml=ml))
    got = {
        r["node_id"]: r
        for r in rows
        if r.get("node_set") and len(r["node_set"]) == 1 and r.get("feasible")
    }
    assert got["node_002"]["rate_items_s"] == raw_rate["node_002"]
    for node_id, row in got.items():
        if node_id != "node_002":
            assert row["rate_items_s"] == pytest.approx(
                2.0 * raw_rate[node_id], rel=1e-5
            )


def test_every_split_row_names_the_model_and_no_row_claims_a_stale_rate() -> None:
    rules = {(t, None): Rule(y_hat=-0.4) for t in ML_TERMS}
    request, view, now = _inputs(True, forced=True)
    ml, _ = stub_ml(rules)
    decision = plan(request, view, "cost_model", now, ml=ml)
    rows = candidates_json(decision)
    assert rows and all(r["ml_model_version"] == "stub-v1" for r in rows)
    split_rows = [r for r in rows if r.get("mode") == "split"]
    assert split_rows
    for r in split_rows:
        combine = r["estimate"]["terms"]["combine"]["detail"]["ml"]
        assert combine["used"] is True and combine["y_hat"] == pytest.approx(-0.4)


def test_ml_is_used_on_a_pinned_whole_job_and_a_single_node_fabric() -> None:
    for n in (1, 2):
        request, snapshot, now = synthetic_fabric(n, split=False, seed=3)
        view = project(request, snapshot)
        ml, _ = stub_ml({("compute", None): Rule(y_hat=0.2)})
        decision = plan(request, view, "cost_model", now, ml=ml)
        assert decision.chosen is not None and decision.ml is not None


def test_no_feasible_candidate_asks_nothing() -> None:
    request, snapshot, now = synthetic_fabric(3, split=False, seed=3)
    snapshot = dataclasses.replace(
        snapshot,
        nodes=tuple(dataclasses.replace(n, state="offline") for n in snapshot.nodes),
    )
    view = project(request, snapshot)
    ml, stub = stub_ml({("compute", None): Rule()})
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.chosen is None
    assert stub.calls == 0
    assert decision.ml is not None and decision.ml.error is None


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_every_placement_policy_still_plans_with_a_model(split: bool) -> None:
    from meshforge.common.manifest import POLICIES

    request, view, now = _inputs(split)
    for policy in POLICIES:
        ml, _ = stub_ml({(t, None): Rule(y_hat=-0.2) for t in ML_TERMS})
        decision = plan(request, view, policy, now, ml=ml)
        assert decision.ml is not None and decision.ml.error is None, policy
        assert decision.chosen is not None, policy
