"""V0.15 G4 step 0 (D362, the G1 carry): **replay-vs-live feature parity**.

The planner feeds the model features built from the live request + snapshot
and the **pre-calibration** analytical term (``planner/predictor.stored_raw``
of the raw seconds). Training rows are built later from what was stored: the
candidate rows (``candidates_json``, through a JSON round trip like
``jsonb``) and the replay (``to_replay`` -> canonical JSON). The two must be
the same numbers, bit for bit (NaN = NaN), for every candidate and every
term - else the model is served inputs it was never trained on.

A recording stub predictor (every term has a model, a narrow interval, a
non-zero ``y_hat``) captures what the planner sent; a calibration with
factor 2 on every term and an OOD node make sure every stored form is met:
a term the model corrected (``detail.ml.raw_s``), a calibrated one
(``detail.calibration.raw_s``) and a raw one.

* (a) every query, against the features the dataset code would compute from
  the stored row of that candidate (``features_from_replay`` + the targets'
  own row readers: ``raw_prediction``, ``_plan_shape``, ``_one_lane_row``,
  ``_raw_seconds_per_item``) - Mode 1, a split job's lanes and every
  candidate's ``combine``;
* (b) end to end: the real dataset builder (``dataset.build``) on the chosen
  candidate's outcome gives rows whose features equal the queries'.

**Known, documented difference (progress note, D362):** a *set* decision's
lane rows carry the set's ``job_lanes`` / ``job_chunk_items`` in the
dataset, while the planner describes a lane as its one-lane candidate (the
set's lane count and chunk size depend on the corrected lane rates
themselves). (b) checks those two features exactly for a one-lane decision
and every other feature for a set decision.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from mf_ml_fixtures import T0
from mf_ml_stub import Rule, StubPredictor, stub_ml
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.ml.dataset import build
from meshforge.controller.ml.features import Predicted, features_from_replay
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.ml.targets import (
    ChunkOutcome,
    CombineOutcome,
    LaneOutcome,
    RunInput,
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
from meshforge.controller.planner.predictor import ML_TERMS, TermQuery
from meshforge.controller.planner.replay import canonical_json, project, to_replay
from meshforge.controller.planner.types import Decision

pytestmark = pytest.mark.unit

_SKEWED = ("job_lanes", "job_chunk_items")


def _key(values: Sequence[float]) -> tuple[str, ...]:
    """Bit-for-bit, NaN-aware."""
    return tuple("nan" if math.isnan(v) else float(v).hex() for v in values)


def _calibration() -> Calibration:
    two = Factor(factor=2.0, n=10, median=2.0)
    return Calibration(
        version=3,
        terms={
            t: TermFactors(global_=two)
            for t in ("transfer", "env", "compute", "output")
        },
    )


def _learnable(
    split: bool, *, n: int = 4, forced: bool = False
) -> tuple[Any, Any, Any]:
    request, snapshot, now = synthetic_fabric(n, split=split, seed=n + 11)
    snapshot = dataclasses.replace(
        snapshot,
        config=dataclasses.replace(snapshot.config, default_env_s=30.0),
    )
    request = dataclasses.replace(
        request,
        history=dataclasses.replace(
            request.history, output_bytes=5e6, output_samples=3
        ),
    )
    if forced:
        request = dataclasses.replace(
            request, node_set=tuple(node.node_id for node in snapshot.nodes)
        )
    return request, project(request, snapshot), now


def _rules(ood_node: str) -> dict[tuple[str, str | None], Rule]:
    rules: dict[tuple[str, str | None], Rule] = {
        (term, None): Rule(y_hat=0.2, half_width=0.05) for term in ML_TERMS
    }
    for term in ML_TERMS:
        rules[(term, ood_node)] = Rule(y_hat=0.2, ood=True, ood_reason="knn")
    return rules


@dataclasses.dataclass(frozen=True)
class Live:
    request: Any
    now: Any
    decision: Decision
    rows: list[dict[str, Any]]
    replay: dict[str, Any]
    queries: list[TermQuery]


def _live(split: bool, *, forced: bool = False) -> Live:
    request, view, now = _learnable(split, forced=forced)
    ml, stub = stub_ml(_rules("node_001"))
    decision = plan(request, view, "cost_model", now, calibration=_calibration(), ml=ml)
    assert decision.ml is not None and decision.ml.error is None
    rows = json.loads(json.dumps(candidates_json(decision)))  # like jsonb
    replay = json.loads(
        canonical_json(
            to_replay(request, view, now, calibration=_calibration(), ml=decision.ml)
        )
    )
    return Live(request, now, decision, rows, replay, list(stub.queries))


def _terms(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return row["estimate"]["terms"]


def _predicted(term: Mapping[str, Any], row: Mapping[str, Any]) -> Predicted:
    lanes, chunk = _plan_shape(row)
    raw = raw_prediction(term)
    assert raw is not None
    return Predicted(
        raw_s=raw,
        source=term["source"],
        detail=term["detail"],
        lanes=lanes,
        chunk_items=chunk,
    )


def _stored_features(
    replay: Mapping[str, Any], node_id: str, p: Predicted
) -> tuple[str, ...]:
    values = features_from_replay(replay, node_id, p)
    return _key([values[name] for name in NUMERIC_FEATURES])


# ------------------------------------------------------------ (a) per candidate


def test_mode1_every_candidate_and_term_matches_its_stored_row() -> None:
    live = _live(split=False)
    assert {q.term for q in live.queries} >= {"env", "compute", "output"}
    by_node = {r["node_id"]: r for r in live.rows if r["feasible"]}
    seen = 0
    for q in live.queries:
        row = by_node[q.node_id]
        term = _terms(row)[q.term]
        expected = _stored_features(live.replay, q.node_id, _predicted(term, row))
        assert _key(q.features) == expected, (q.term, q.node_id)
        seen += 1
    assert seen == len(live.queries) > 0
    # every stored form was met: corrected, calibrated (OOD node), raw source
    blocks = [
        _terms(r)[t]["detail"]
        for r in by_node.values()
        for t in ("env", "compute", "output")
    ]
    assert any(b["ml"]["used"] and "calibration" not in b for b in blocks)
    assert any(not b["ml"]["used"] and "calibration" in b for b in blocks)


@pytest.mark.parametrize("forced", [False, True], ids=["auto", "forced_set"])
def test_mode3_lane_and_combine_terms_match_their_stored_rows(forced: bool) -> None:
    live = _live(split=True, forced=forced)
    terms = Counter(q.term for q in live.queries)
    assert terms["lane_env"] and terms["chunk_rate"] and terms["combine"]
    outcome = DecisionOutcome(
        decision_id=1,
        attempt_id="a",
        chosen_node_id=None,
        attempt_node_id="x",
        attempt_state="done",
        attempt_role="lane",
        candidates=live.rows,
        phases_ms={},
        finished_at=T0,
    )
    combines: list[tuple[str, ...]] = []
    for q in live.queries:
        if q.term == "combine":
            combines.append(_key(q.features))
            continue
        row = _one_lane_row(outcome, q.node_id)
        assert row is not None
        if q.term == "chunk_rate":
            raw = _raw_seconds_per_item(row)
            assert raw is not None
            lanes, chunk = _plan_shape(row)
            p = Predicted(
                raw_s=raw,
                source=row["rate_source"],
                detail={},
                lanes=lanes,
                chunk_items=chunk,
            )
        else:
            name = "transfer" if q.term == "lane_stage" else "env"
            p = _predicted(_terms(row)[name], row)
        assert _key(q.features) == _stored_features(live.replay, q.node_id, p), (
            q.term,
            q.node_id,
        )
    expected = [
        _stored_features(
            live.replay,
            _terms(r)["combine"]["detail"]["combine_node_id"],
            _predicted(_terms(r)["combine"], r),
        )
        for r in live.rows
        if r["feasible"] and r.get("estimate") is not None
    ]
    assert Counter(combines) == Counter(expected)


# ------------------------------------------------------------ (b) end to end


def _query(queries: Sequence[TermQuery], term: str, node_id: str) -> TermQuery:
    found = [q for q in queries if q.term == term and q.node_id == node_id]
    assert len(found) == 1, (term, node_id, len(found))
    return found[0]


def _without_skew(values: Sequence[str]) -> list[str]:
    return [
        v
        for name, v in zip(NUMERIC_FEATURES, values, strict=True)
        if name not in _SKEWED
    ]


def test_dataset_rows_of_a_whole_job_equal_the_live_queries() -> None:
    live = _live(split=False)
    chosen = live.decision.chosen
    assert chosen is not None
    run = RunInput(
        outcome=DecisionOutcome(
            decision_id=1,
            attempt_id="att_1",
            chosen_node_id=chosen.node_id,
            attempt_node_id=chosen.node_id,
            attempt_state="done",
            attempt_role="run",
            candidates=live.rows,
            phases_ms={
                "queue": 5,
                "stage": 9000,
                "env": 31000,
                "run": 45000,
                "collect": 900,
            },
            finished_at=T0,
            family="cpu.primes",
            fingerprint="fp",
        ),
        job_id="job_a",
        replay=live.replay,
    )
    dataset = build([run])
    assert dataset.rows, dataset.counts
    for row in dataset.rows:
        q = _query(live.queries, row["term"], row["node_id"])
        assert _key([row[n] for n in NUMERIC_FEATURES]) == _key(q.features), row["term"]


def _lanes_of(row: Mapping[str, Any]) -> tuple[LaneOutcome, ...]:
    return tuple(
        LaneOutcome(
            attempt_id=f"lane_{i}",
            node_id=node_id,
            state="done",
            phases_ms={"stage": 2000, "env": 31000, "run": 30_000, "collect": 50},
            finished_at=T0,
            chunks=(ChunkOutcome(items=50, run_ms=6_000, outcome="committed"),),
        )
        for i, node_id in enumerate(row["node_set"])
    )


@pytest.mark.parametrize(
    "forced", [False, True], ids=["one_lane_or_auto", "forced_set"]
)
def test_dataset_rows_of_a_split_job_equal_the_live_queries(forced: bool) -> None:
    live = _live(split=True, forced=forced)
    chosen_row = next(r for r in live.rows if r.get("rank") == 1)
    lanes = _lanes_of(chosen_row)
    combine_term = _terms(chosen_row)["combine"]
    run = RunInput(
        outcome=DecisionOutcome(
            decision_id=1,
            attempt_id="lane_0",
            chosen_node_id=chosen_row["node_id"],
            attempt_node_id=chosen_row["node_id"],
            attempt_state="done",
            attempt_role="lane",
            candidates=live.rows,
            phases_ms=dict(lanes[0].phases_ms),
            finished_at=T0,
            family="cpu.split",
            fingerprint="fp",
        ),
        job_id="job_a",
        replay=live.replay,
        lanes=lanes,
        combine=CombineOutcome(
            attempt_id="combine_1",
            node_id=combine_term["detail"]["combine_node_id"],
            state="done",
            phases_ms={"stage": 400, "env": None, "run": 1600, "collect": 30},
            finished_at=T0,
        ),
    )
    dataset = build([run])
    is_set = chosen_row["mode"] == "split"
    assert is_set is forced or not forced
    by_term = Counter(row["term"] for row in dataset.rows)
    assert by_term["chunk_rate"] and by_term["combine"], dataset.counts
    for row in dataset.rows:
        got = _key([row[n] for n in NUMERIC_FEATURES])
        if row["term"] == "combine":
            assert got in {
                _key(q.features) for q in live.queries if q.term == "combine"
            }
            continue
        q = _query(live.queries, row["term"], row["node_id"])
        if is_set:
            assert _without_skew(got) == _without_skew(_key(q.features)), row["term"]
        else:
            assert got == _key(q.features), row["term"]


def test_the_features_never_see_a_calibrated_or_corrected_raw() -> None:
    """D362 directly: the queries' ``an_raw_s`` is the pre-calibration,
    pre-ML analytical seconds - never the stored (scaled) ``seconds``."""
    live = _live(split=False)
    request, view, now = _learnable(False)  # the same inputs, no model
    raw_decision = plan(request, view, "cost_model", now)
    raw = {
        (c.node_id, name): round(getattr(c.estimate, name).seconds, 6)
        for c in raw_decision.candidates
        if c.estimate is not None
        for name in ("transfer", "env", "compute", "output")
    }
    col = NUMERIC_FEATURES.index("an_raw_s")
    for q in live.queries:
        assert q.features[col] == raw[(q.node_id, q.term)]
    stored = {
        (r["node_id"], t): _terms(r)[t]["seconds"]
        for r in live.rows
        if r["feasible"]
        for t in ("env", "compute", "output")
    }
    assert any(stored[k] != raw[k] for k in stored)  # the stored ones were scaled


def test_a_stub_answer_is_recorded_once_per_query() -> None:
    stub = StubPredictor(rules={("compute", None): Rule()})
    assert (
        stub.predict([TermQuery("compute", "n", (1.0,))])[0].model_version == "stub-v1"
    )
    assert stub.calls == 1 and len(stub.queries) == 1
