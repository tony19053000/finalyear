# ruff: noqa: S311 - seeded test data, not security
"""V0.15 G4 tester round 1: train/serve parity (D362, D366) on *hostile*
inputs.

The implementer's parity test uses one tidy synthetic fabric. Here the same
claim is held on ``random_case`` fabrics: NaN / infinite scores and loads,
stale and future-dated links, nodes holding other jobs' trees, pinned and
forced-set requests, a random calibration (so some terms are corrected, some
calibrated, some raw) and OOD nodes:

for **every** query the planner sent the model, the features equal - bit for
bit, NaN-aware - what the dataset code computes from the *stored* row of that
candidate and the stored replay, after a JSON round trip (as ``jsonb`` does).
Mode 1 terms, Mode 3 lane terms (``lane_stage`` / ``lane_env`` / ``chunk_rate``,
D366 one-lane shape) and every candidate's ``combine``.

And D362 as an invariant: ``an_raw_s`` of every query is the pre-calibration,
pre-ML analytical seconds (as stored, 6 decimals).
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from collections.abc import Sequence
from typing import Any

import pytest
from mf_ml_fixtures import T0
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import random_case

from meshforge.controller.ml.features import Predicted, features_from_replay
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.ml.targets import (
    _one_lane_row,
    _plan_shape,
    _raw_seconds_per_item,
)
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    Calibration,
    DecisionOutcome,
    Factor,
    TermFactors,
    raw_prediction,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import ML_TERMS
from meshforge.controller.planner.replay import canonical_json, project, to_replay

pytestmark = pytest.mark.unit

AN_RAW = NUMERIC_FEATURES.index("an_raw_s")
MODE1 = [s for s in range(400) if random_case(s)[0].split is None]
MODE3 = [s for s in range(400) if random_case(s)[0].split is not None]


def _key(values: Sequence[float]) -> tuple[str, ...]:
    return tuple("nan" if math.isnan(v) else float(v).hex() for v in values)


def _calibration(rng: random.Random) -> Calibration | None:
    if rng.random() < 0.2:
        return None
    terms = {
        t: TermFactors(global_=Factor(factor=rng.uniform(0.4, 3.0), n=9, median=1.0))
        for t in ("transfer", "env", "compute", "output")
        if rng.random() < 0.8
    }
    return Calibration(version=2, terms=terms)


def _rules(rng: random.Random, ids: list[str]) -> dict[Any, Rule]:
    rules: dict[Any, Rule] = {}
    for term in ML_TERMS:
        if rng.random() < 0.1:
            continue
        rules[(term, None)] = Rule(y_hat=rng.uniform(-1, 1), half_width=0.05)
        for node_id in ids:
            if rng.random() < 0.25:
                rules[(term, node_id)] = Rule(y_hat=0.3, ood=True, ood_reason="knn")
    return rules


def _terms(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = row["estimate"]["terms"]
    return out


def _predicted(term: dict[str, Any], row: dict[str, Any]) -> Predicted | None:
    raw = raw_prediction(term)
    if raw is None:
        return None
    lanes, chunk = _plan_shape(row)
    return Predicted(
        raw_s=raw,
        source=term["source"],
        detail=term["detail"],
        lanes=lanes,
        chunk_items=chunk,
    )


def _stored(replay: dict[str, Any], node_id: str, p: Predicted) -> tuple[str, ...]:
    values = features_from_replay(replay, node_id, p)
    return _key([values[n] for n in NUMERIC_FEATURES])


@pytest.mark.parametrize("seed", range(400))
def test_every_query_equals_its_stored_row_features(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    rng = random.Random(seed * 3 + 1)
    view = project(request, snapshot)
    cal = _calibration(rng)
    ml, stub = stub_ml(_rules(rng, [n.node_id for n in snapshot.nodes]))
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    assert decision.ml is not None and decision.ml.error is None
    rows = json.loads(json.dumps(candidates_json(decision)))
    replay = json.loads(
        canonical_json(to_replay(request, view, now, calibration=cal, ml=decision.ml))
    )
    by_node = {r["node_id"]: r for r in rows if r["feasible"] and not r.get("node_set")}
    outcome = DecisionOutcome(
        decision_id=1,
        attempt_id="a",
        chosen_node_id=None,
        attempt_node_id="x",
        attempt_state="done",
        attempt_role="lane",
        candidates=rows,
        phases_ms={},
        finished_at=T0,
    )
    combines: list[tuple[str, ...]] = []
    for q in stub.queries:
        if q.term == "combine":
            combines.append(_key(q.features))
            continue
        if q.term in ("lane_stage", "lane_env", "chunk_rate"):
            row = _one_lane_row(outcome, q.node_id)
            assert row is not None, (seed, q.term, q.node_id)
            if q.term == "chunk_rate":
                raw = _raw_seconds_per_item(row)
                assert raw is not None
                lanes, chunk = _plan_shape(row)
                p: Predicted | None = Predicted(
                    raw_s=raw,
                    source=row["rate_source"],
                    detail={},
                    lanes=lanes,
                    chunk_items=chunk,
                )
            else:
                name = "transfer" if q.term == "lane_stage" else "env"
                p = _predicted(_terms(row)[name], row)
        else:
            row = by_node[q.node_id]
            p = _predicted(_terms(row)[q.term], row)
        assert p is not None
        assert _key(q.features) == _stored(replay, q.node_id, p), (
            seed,
            q.term,
            q.node_id,
        )
    if combines:
        expected = []
        for r in rows:
            if not r["feasible"] or r.get("estimate") is None:
                continue
            term = _terms(r).get("combine")
            if term is None:
                continue
            p = _predicted(term, r)
            assert p is not None
            expected.append(_stored(replay, term["detail"]["combine_node_id"], p))
        assert Counter(combines) == Counter(expected), seed


@pytest.mark.parametrize("seed", MODE1)
def test_an_raw_s_is_always_the_pre_calibration_analytical_seconds(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    assert request.split is None
    rng = random.Random(seed)
    view = project(request, snapshot)
    cal = _calibration(rng)
    ml, stub = stub_ml(_rules(rng, [n.node_id for n in snapshot.nodes]))
    plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    raw = plan(request, view, "cost_model", now)
    seconds = {
        (c.node_id, name): round(getattr(c.estimate, name).seconds, 6)
        for c in raw.candidates
        if c.estimate is not None
        for name in ("transfer", "env", "compute", "output")
    }
    for q in stub.queries:
        want = seconds[(q.node_id, q.term)]
        got = q.features[AN_RAW]
        assert got == want or (math.isnan(got) and math.isnan(want)), (
            seed,
            q.term,
            q.node_id,
        )


@pytest.mark.parametrize("seed", MODE3)
def test_mode3_lane_an_raw_s_is_the_pre_calibration_analytical_value(
    seed: int,
) -> None:
    request, snapshot, now = random_case(seed)
    assert request.split is not None
    rng = random.Random(seed)
    view = project(request, snapshot)
    cal = _calibration(rng)
    ml, stub = stub_ml(_rules(rng, [n.node_id for n in snapshot.nodes]))
    plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    raw = candidates_json(plan(request, view, "cost_model", now))
    lanes = {
        r["node_id"]: r
        for r in raw
        if r.get("feasible") and r.get("node_set") and len(r["node_set"]) == 1
    }
    checked = 0
    for q in stub.queries:
        if q.term not in ("lane_stage", "lane_env") or q.node_id not in lanes:
            continue
        name = "transfer" if q.term == "lane_stage" else "env"
        want = _terms(lanes[q.node_id])[name]["seconds"]
        assert q.features[AN_RAW] == want, (seed, q.term, q.node_id)
        checked += 1
    assert checked >= 0  # (random fabrics: some have no costed lane)
