# ruff: noqa: S311 - seeded test data, not security
"""V0.15 G4 tester round 1: an independent oracle for the D336 precedence.

Derived from the brief (§2 D336, D345, D363; §5d step 2), not from the
implementation: for hundreds of random planner cases (``random_case``: offline
nodes, odd numbers, forced sets, ...) and random per-term / per-node stub
rules, every learnable term of every costed candidate must be

* exact-by-construction (a structural 0 s: a transfer with nothing missing,
  an env that is cached) -> never queried, reason ``exact_by_construction``;
* else no model -> ``no_model``; raw below the dataset floor ->
  ``below_min_predicted``; OOD -> ``ood``; interval wider than the limit ->
  ``interval_too_wide``; else ML: ``raw x exp(y_hat)``;
* any non-ML term equals the **calibrated** V0.14 term (never the raw one
  when a factor exists);

and the set of (term, node) pairs the predictor was *asked* about is exactly
the set whose reason is ML / OOD / wide - one call, in Mode 1.

Also: a predictor that fails (raises, malformed, second-call failure in
Mode 3) yields the calibrated V0.14 decision field for field; a model that
knows no term (or answers everything OOD) changes only the ML records.
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

import pytest
from mf_ml_stub import Rule, StubPredictor, stub_ml
from mf_planner_fabrics import random_case

from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    Calibration,
    Factor,
    TermFactors,
)
from meshforge.controller.planner.decision_json import candidates_json, split_json
from meshforge.controller.planner.predictor import ML_TERMS, MlInput
from meshforge.controller.planner.replay import canonical_json, project, to_replay
from meshforge.controller.planner.types import Decision

pytestmark = pytest.mark.unit

WHOLE = ("transfer", "env", "compute", "output")
FLOOR = 0.05
LIMIT = math.log(2.0)
SEEDS = range(300)
SEEDS_MODE1 = [s for s in SEEDS if random_case(s)[0].split is None]
SEEDS_MODE3 = [s for s in SEEDS if random_case(s)[0].split is not None]


def _calibration(rng: random.Random) -> Calibration | None:
    if rng.random() < 0.25:
        return None
    terms = {}
    for name in WHOLE:
        if rng.random() < 0.85:
            f = Factor(factor=rng.uniform(0.3, 3.0), n=10, median=1.0)
            terms[name] = TermFactors(global_=f)
    return Calibration(version=3, terms=terms)


def _rules(rng: random.Random, node_ids: list[str]) -> dict[Any, Rule]:
    rules: dict[Any, Rule] = {}
    for term in ML_TERMS:
        if rng.random() < 0.15:
            continue  # no model for this term

        def one() -> Rule:
            return Rule(
                y_hat=rng.uniform(-2.0, 2.0),
                half_width=rng.choice([0.05, 0.3, 0.3466, 0.35, 0.5, 2.0]),
                ood=rng.random() < 0.2,
                ood_reason="knn",
            )

        rules[(term, None)] = one()
        for node_id in node_ids:
            if rng.random() < 0.3:
                rules[(term, node_id)] = one()
    return rules


def _exact(term: str, detail: dict[str, Any]) -> bool:
    """D345, written from the brief."""
    if term in ("transfer", "lane_stage"):
        return detail.get("missing_bytes") == 0
    if term in ("env", "lane_env"):
        return detail.get("env_cached") is True
    return False


def _expected(
    term: str, raw_seconds: float, detail: dict[str, Any], rule: Rule | None
) -> str:
    if _exact(term, detail):
        return "exact_by_construction"
    if rule is None:
        return "no_model"
    if not raw_seconds >= FLOOR:
        return "below_min_predicted"
    if rule.ood:
        return "ood"
    if not 2 * rule.half_width <= LIMIT:
        return "interval_too_wide"
    return "used"


def _rule(rules: dict[Any, Rule], term: str, node_id: str) -> Rule | None:
    return rules.get((term, node_id)) or rules.get((term, None))


def _singles(decision: Decision) -> dict[str, Any]:
    return {
        c.node_id: c
        for c in decision.candidates
        if c.feasible and c.estimate is not None and not c.node_set
    }


@pytest.mark.parametrize("seed", SEEDS_MODE1)
def test_mode1_precedence_matches_an_independent_oracle(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    assert request.split is None
    rng = random.Random(seed * 7 + 1)
    view = project(request, snapshot)
    cal = _calibration(rng)
    ids = [n.node_id for n in snapshot.nodes]
    rules = _rules(rng, ids)
    ml, stub = stub_ml(rules)

    raw = plan(request, view, "cost_model", now)
    calibrated = plan(request, view, "cost_model", now, calibration=cal)
    got = plan(request, view, "cost_model", now, calibration=cal, ml=ml)

    assert got.ml is not None and got.ml.error is None
    assert stub.calls <= 1
    mine, base, want = _singles(got), _singles(raw), _singles(calibrated)
    assert set(mine) == set(base)
    asked: set[tuple[str, str]] = set()
    for node_id, row in mine.items():
        for term in WHOLE:
            r_term = getattr(base[node_id].estimate, term)
            m_term = getattr(row.estimate, term)
            block = m_term.detail["ml"]
            rule = _rule(rules, term, node_id)
            expected = _expected(term, r_term.seconds, dict(r_term.detail), rule)
            if expected == "used":
                assert block["used"] is True, (seed, node_id, term)
                assert rule is not None
                assert (
                    m_term.seconds
                    == pytest.approx(
                        r_term.seconds * math.exp(rule.y_hat), rel=1e-9, abs=1e-9
                    )
                    or m_term.seconds >= 1e8
                )  # capped term
                assert m_term.source == r_term.source
                assert "calibration" not in m_term.detail
                assert block["raw_s"] == round(r_term.seconds, 6)
            else:
                assert block["used"] is False
                assert block["reason"] == expected, (seed, node_id, term)
                c_term = getattr(want[node_id].estimate, term)
                assert m_term.seconds == c_term.seconds, (seed, node_id, term)
                assert m_term.detail.get("calibration") == c_term.detail.get(
                    "calibration"
                )
            if expected in ("used", "ood", "interval_too_wide"):
                asked.add((term, node_id))
    queried = {(q.term, q.node_id) for q in stub.queries}
    assert queried == asked, (seed, queried ^ asked)


def _strip_ml(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        row = {
            k: v for k, v in row.items() if k not in ("ml_model_version", "ml_fallback")
        }
        estimate = row.get("estimate")
        if isinstance(estimate, dict):
            terms = {
                name: {
                    **term,
                    "detail": {k: v for k, v in term["detail"].items() if k != "ml"},
                }
                for name, term in estimate["terms"].items()
            }
            row["estimate"] = {**estimate, "terms": terms}
        # a lane rate's ml block lives in the row's compute detail only for
        # one-lane rows; nothing else carries it
        out.append(row)
    return out


def _same_but_ml(a: Decision, b: Decision) -> None:
    assert [(c.node_id, c.node_set, c.rank, c.score) for c in a.candidates] == [
        (c.node_id, c.node_set, c.rank, c.score) for c in b.candidates
    ]
    assert a.chosen == b.chosen or (
        a.chosen is not None
        and b.chosen is not None
        and (a.chosen.node_id, a.chosen.node_set, a.chosen.rank)
        == (b.chosen.node_id, b.chosen.node_set, b.chosen.rank)
    )
    assert a.reason_key == b.reason_key
    assert (None if a.split is None else split_json(a.split)) == (
        None if b.split is None else split_json(b.split)
    )
    assert _strip_ml(candidates_json(a)) == _strip_ml(candidates_json(b))


class _AllOod(StubPredictor):
    pass


@pytest.mark.parametrize("seed", SEEDS)
def test_a_model_that_trusts_nothing_is_the_calibrated_v014_decision(
    seed: int,
) -> None:
    """Every term OOD / too wide / no model: identical times, ranks, picks
    and split plans to the calibrated planner (whole and split)."""
    request, snapshot, now = random_case(seed)
    rng = random.Random(seed)
    view = project(request, snapshot)
    cal = _calibration(rng)
    rules: dict[Any, Rule] = {}
    for term in ML_TERMS:
        kind = rng.choice(["ood", "wide", "none"])
        if kind == "ood":
            rules[(term, None)] = Rule(y_hat=1.0, ood=True)
        elif kind == "wide":
            rules[(term, None)] = Rule(y_hat=-1.0, half_width=5.0)
    ml, _ = stub_ml(rules)
    base = plan(request, view, "cost_model", now, calibration=cal)
    got = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    assert got.ml is not None and got.ml.error is None
    _same_but_ml(got, base)


class _FailsOnCall(StubPredictor):
    """Answers the first ``ok_calls`` calls, then fails (Mode 3's second call)."""

    def __init__(self, ok_calls: int, how: str) -> None:
        super().__init__(rules={(t, None): Rule(y_hat=-0.7) for t in ML_TERMS})
        self.ok_calls = ok_calls
        self.how = how

    def predict(self, rows: Any) -> Any:
        if self.calls >= self.ok_calls:
            self.calls += 1
            if self.how == "raise":
                raise TimeoutError("late")
            if self.how == "short":
                return []
            return [None] * len(rows)  # not predictions at all
        return super().predict(rows)


@pytest.mark.parametrize("how", ["raise", "short", "junk"])
@pytest.mark.parametrize("ok_calls", [0, 1])
@pytest.mark.parametrize("seed", range(60))
def test_any_failing_call_gives_the_calibrated_decision_field_for_field(
    seed: int, ok_calls: int, how: str
) -> None:
    """Mode 3 asks twice: a failure on the *second* call must discard the
    lane corrections of the first (the whole decision is the fallback)."""
    request, snapshot, now = random_case(seed)
    rng = random.Random(seed)
    view = project(request, snapshot)
    cal = _calibration(rng)
    stub = _FailsOnCall(ok_calls, how)
    from meshforge.controller.ml.features import term_features

    ml = MlInput(predictor=stub, features=term_features)
    base = plan(request, view, "cost_model", now, calibration=cal)
    got = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    if stub.calls <= ok_calls:  # the failing call was never reached
        assert got.ml is not None and got.ml.error is None
        return
    assert got.ml is not None and got.ml.error is not None
    _same_but_ml(got, base)
    rows = candidates_json(got)
    assert rows and all(r["ml_fallback"] == "predictor_error" for r in rows)
    # no row claims a model correction
    for row in rows:
        est = row.get("estimate")
        if est:
            for term in est["terms"].values():
                block = term["detail"].get("ml")
                assert block is None or block["used"] is False


@pytest.mark.parametrize("seed", range(120))
def test_every_ml_decision_serialises_finite_and_replays_to_itself(seed: int) -> None:
    from mf_planner_golden import decision_view

    from meshforge.controller.ml.replay import replay_decision

    request, snapshot, now = random_case(seed)
    rng = random.Random(seed)
    view = project(request, snapshot)
    cal = _calibration(rng)
    rules = _rules(rng, [n.node_id for n in snapshot.nodes])
    ml, _ = stub_ml(rules)
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    text = canonical_json(
        to_replay(request, view, now, calibration=cal, ml=decision.ml)
    )
    replay = json.loads(text)
    for payload in (candidates_json(decision),):
        blob = canonical_json(payload)
        assert "NaN" not in blob and "Infinity" not in blob
    # the stored replay carries the calibration block; replay uses it
    result = replay_decision(replay, "cost_model", load=lambda v: ml)
    assert result.status == "ok" and result.decision is not None
    assert decision_view(result.decision) == decision_view(decision)
    gone = replay_decision(replay, "cost_model", load=lambda _v: None)
    assert gone.status == "ml_model_unavailable" and gone.decision is None


def _lanes(decision: Decision) -> dict[str, dict[str, Any]]:
    """Stored one-lane candidate rows by node id."""
    return {
        r["node_id"]: r
        for r in candidates_json(decision)
        if r.get("feasible") and r.get("node_set") and len(r["node_set"]) == 1
    }


@pytest.mark.parametrize("seed", SEEDS_MODE3)
def test_mode3_lane_precedence_matches_an_oracle(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    assert request.split is not None
    rng = random.Random(seed * 11 + 5)
    view = project(request, snapshot)
    cal = _calibration(rng)
    rules = _rules(rng, [n.node_id for n in snapshot.nodes])
    ml, stub = stub_ml(rules)
    raw = _lanes(plan(request, view, "cost_model", now))
    calibrated = _lanes(plan(request, view, "cost_model", now, calibration=cal))
    got_decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    assert got_decision.ml is not None and got_decision.ml.error is None
    got = _lanes(got_decision)
    assert stub.calls <= 2
    asked: set[tuple[str, str]] = set()
    for node_id, row in got.items():
        if node_id not in raw:
            continue
        for lane_term, name in (("lane_stage", "transfer"), ("lane_env", "env")):
            r_term = raw[node_id]["estimate"]["terms"][name]
            m_term = row["estimate"]["terms"][name]
            block = m_term["detail"]["ml"]
            rule = _rule(rules, lane_term, node_id)
            expected = _expected(
                lane_term, r_term["seconds"], dict(r_term["detail"]), rule
            )
            if expected == "used":
                assert block["used"] is True, (seed, node_id, lane_term)
                assert rule is not None
                assert (
                    m_term["seconds"]
                    == pytest.approx(
                        r_term["seconds"] * math.exp(rule.y_hat), rel=1e-5, abs=2e-6
                    )
                    or m_term["seconds"] >= 1e8
                )
                assert "calibration" not in m_term["detail"]
            else:
                assert block["used"] is False
                assert block["reason"] == expected, (seed, node_id, lane_term)
                c_term = calibrated[node_id]["estimate"]["terms"][name]
                assert m_term["seconds"] == c_term["seconds"], (
                    seed,
                    node_id,
                    lane_term,
                )
            if expected in ("used", "ood", "interval_too_wide"):
                asked.add((lane_term, node_id))
        rate_block = row["estimate"]["terms"]["compute"]["detail"].get("ml")
        assert rate_block is not None, (seed, node_id)
        rate_rule = _rule(rules, "chunk_rate", node_id)
        r_rate = raw[node_id]["rate_items_s"]
        want = (
            "no_model"
            if rate_rule is None
            else "ood"
            if rate_rule.ood
            else "interval_too_wide"
            if not 2 * rate_rule.half_width <= LIMIT
            else "used"
        )
        if r_rate is None or not r_rate > 0:
            continue
        if want == "used":
            assert rate_block["used"] is True, (seed, node_id)
            assert rate_rule is not None
            assert row["rate_items_s"] == pytest.approx(
                r_rate / math.exp(rate_rule.y_hat), rel=1e-4
            ), (seed, node_id)
        else:
            assert rate_block["used"] is False
            assert rate_block["reason"] == want
            assert row["rate_items_s"] == calibrated[node_id]["rate_items_s"]
        if want in ("used", "ood", "interval_too_wide"):
            asked.add(("chunk_rate", node_id))
    queried = {(q.term, q.node_id) for q in stub.queries if q.term != "combine"}
    assert asked <= queried, (seed, asked - queried)
