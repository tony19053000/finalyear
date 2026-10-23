"""V0.15 G1 (D333, D335): residual targets and every exclusion reason.

Real stored decisions (the planner on a 3-node fabric) with hand-made
outcomes: ``y = ln(actual / raw)`` is checked against the stored raw
prediction for every term - whole job (Mode 1, Local), a calibrated decision
(raw = ``detail.calibration.raw_s``), a one-lane split and a Mode 3 set
(lanes, ``chunk_rate`` arithmetic, ``combine``) - and each skip reason is
produced by exactly the input it names.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import math
from typing import Any

import pytest
from mf_ml_fixtures import chosen, lane_row, set_run, stored, whole_run

from meshforge.controller.ml.schema import TERM_NAMES, TERMS, term
from meshforge.controller.ml.targets import (
    ChunkOutcome,
    CombineOutcome,
    LaneOutcome,
    Observation,
    Skip,
    check,
    exact_by_construction,
    observations,
    residual,
)
from meshforge.controller.planner.calibration import MIN_PREDICTED_S, TERM_PHASE

pytestmark = pytest.mark.unit

PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}


def _by_term(rows: list[Observation]) -> dict[str, Observation]:
    out = {o.term: o for o in rows}
    assert len(out) == len(rows)
    return out


def _skips(counter: Any) -> dict[str, int]:
    return {k: v for k, v in counter.items() if v}


# ------------------------------------------------------------------ basics


def test_residual_is_ln_actual_over_raw() -> None:
    assert residual(10.0, 20.0) == math.log(2.0)
    assert residual(20.0, 10.0) == -math.log(2.0)  # faster than predicted: y < 0
    assert residual(3.0, 3.0) == 0.0


def test_the_term_phase_map_is_the_calibrations() -> None:
    for t in TERMS:
        if t.phase is not None:
            assert t.phase == TERM_PHASE[t.planner_term]
    assert term("lane_stage").phase == TERM_PHASE["transfer"] == "stage"
    assert term("chunk_rate").phase is None
    assert TERM_NAMES == (
        "transfer",
        "env",
        "compute",
        "output",
        "lane_stage",
        "lane_env",
        "chunk_rate",
        "combine",
    )


@pytest.mark.parametrize(
    ("raw", "actual", "reason"),
    [
        (0.0, 5.0, Skip.RAW_NOT_POSITIVE),
        (-1.0, 5.0, Skip.RAW_NOT_POSITIVE),
        (float("nan"), 5.0, Skip.RAW_NOT_POSITIVE),
        (float("inf"), 5.0, Skip.RAW_NOT_POSITIVE),
        (None, 5.0, Skip.RAW_NOT_POSITIVE),
        (10.0, 0.0, Skip.ACTUAL_NOT_POSITIVE),
        (10.0, float("inf"), Skip.ACTUAL_NOT_POSITIVE),
        (10.0, None, Skip.ACTUAL_NOT_POSITIVE),
        (MIN_PREDICTED_S / 2, 5.0, Skip.BELOW_MIN_PREDICTED),
        (MIN_PREDICTED_S, 5.0, None),
        (10.0, 5.0, None),
    ],
)
def test_check_names_each_reason(
    raw: float | None, actual: float | None, reason: Skip | None
) -> None:
    assert check("compute", raw, actual, {}) == reason


# --- D345: exact by construction, by term + detail (never by source alone) ---


@pytest.mark.parametrize(
    ("term", "detail", "exact"),
    [
        # nothing to move: the node holds every tree
        ("transfer", {"missing_bytes": 0}, True),
        ("lane_stage", {"missing_bytes": 0}, True),
        # bytes move - over a probed link (measured bandwidth) or history: learnable
        ("transfer", {"missing_bytes": 200_000_000, "bw_source": "probe"}, False),
        ("lane_stage", {"missing_bytes": 5, "bw_source": "transfer_history"}, False),
        ("transfer", {"missing_bytes": False}, False),  # not a count
        ("transfer", {}, False),  # unknown: not exact
        # the built env is cached: 0 s, exact
        ("env", {"env_cached": True}, True),
        ("lane_env", {"env_cached": True}, True),
        ("env", {"env_cached": False}, False),
        ("env", {"env_cached": 1}, False),  # only the boolean true
        # never exact: derived from rates or history
        ("compute", {"missing_bytes": 0, "env_cached": True}, False),
        ("output", {"out_bytes": 0}, False),
        ("chunk_rate", {}, False),
        ("combine", {"parts_bytes": 0}, False),
    ],
)
def test_exact_by_construction_is_by_term_and_detail(
    term: str, detail: dict[str, Any], exact: bool
) -> None:
    assert exact_by_construction(term, detail) is exact
    assert (check(term, 10.0, 5.0, detail) == Skip.EXACT) is exact


def test_a_measured_source_alone_is_not_exact() -> None:
    """The calibration's rule (``source == measured``) is not the ML rule."""
    assert check("transfer", 10.0, 5.0, {"missing_bytes": 10**8}) is None
    assert check("env", 30.0, 5.0, {"env_cached": False}) is None


def test_transfer_over_a_fresh_probed_link_is_learned() -> None:
    s = stored(pinned="node_001", learnable=False)  # the full probed link matrix
    terms = chosen(s)["estimate"]["terms"]
    transfer = terms["transfer"]
    assert transfer["source"] == "measured"  # measured bandwidth ...
    assert transfer["detail"]["missing_bytes"] > 0  # ... but a predicted time
    rows, skipped = observations(whole_run(s, phases=PHASES))
    obs = _by_term(rows)["transfer"]
    assert obs.raw_s == transfer["seconds"]
    assert obs.y == math.log(12.0 / transfer["seconds"])
    assert obs.source == "measured"  # kept as the source one-hot feature
    assert "exact_by_construction" not in _skips(skipped)


def test_a_cached_env_is_exact() -> None:
    s = _edit_term(
        stored(pinned="node_001"),
        "env",
        seconds=0.0,
        source="measured",
        detail={"env_cached": True, "env_hash": "e" * 64},
    )
    rows, skipped = observations(whole_run(s, phases=PHASES))
    assert "env" not in {o.term for o in rows}
    assert _skips(skipped) == {"exact_by_construction": 1}


# ------------------------------------------------------------------ whole job


def test_mode1_whole_job_every_term_is_ln_actual_over_stored_raw() -> None:
    s = stored(pinned="node_001")
    terms = chosen(s)["estimate"]["terms"]
    rows, skipped = observations(whole_run(s, phases=PHASES))
    got = _by_term(rows)
    assert set(got) == {"transfer", "env", "compute", "output"}
    for name, phase in TERM_PHASE.items():
        raw = terms[name]["seconds"]
        actual = PHASES[phase] / 1000.0
        obs = got[name]
        assert obs.raw_s == raw
        assert obs.actual_s == actual
        assert obs.y == math.log(actual / raw)
        assert obs.node_id == "node_001"
        assert obs.attempt_id == "att_1"
        assert obs.source == terms[name]["source"]
        assert obs.lanes == 1 and obs.chunk_items is None
    assert _skips(skipped) == {}


def test_local_transfer_of_nothing_is_exact_not_learned() -> None:
    s = stored()  # local on node_000: holds the tree, transfer 0 s, 0 bytes
    assert chosen(s)["estimate"]["terms"]["transfer"]["detail"]["missing_bytes"] == 0
    rows, skipped = observations(whole_run(s, phases=PHASES))
    assert {o.term for o in rows} == {"env", "compute", "output"}
    assert _skips(skipped) == {"exact_by_construction": 1}


def test_a_calibrated_decision_learns_against_the_raw_prediction() -> None:
    s = stored(pinned="node_001")
    candidates = copy.deepcopy(s.candidates)
    row = next(r for r in candidates if r["rank"] == 1)
    compute = row["estimate"]["terms"]["compute"]
    raw = compute["seconds"]
    compute["seconds"] = raw * 3.0
    compute["detail"] = {
        **compute["detail"],
        "calibration": {"factor": 3.0, "raw_s": raw},
    }
    s = dataclasses.replace(s, candidates=candidates)
    rows, _ = observations(whole_run(s, phases=PHASES))
    obs = _by_term(rows)["compute"]
    assert obs.raw_s == raw  # never the calibrated seconds: a factor never compounds
    assert obs.y == math.log(400.0 / raw)


@pytest.mark.parametrize(
    ("phases", "reason"),
    [
        ({**PHASES, "env": None}, "skipped_phase"),
        ({k: v for k, v in PHASES.items() if k != "collect"}, "missing_phase"),
        ({**PHASES, "stage": 0}, "actual_not_positive"),
    ],
)
def test_phase_reasons(phases: dict[str, int | None], reason: str) -> None:
    s = stored(pinned="node_001")
    rows, skipped = observations(whole_run(s, phases=phases))
    assert len(rows) == 3
    assert _skips(skipped) == {reason: 1}


def _edit_term(s: Any, name: str, **changes: Any) -> Any:
    candidates = copy.deepcopy(s.candidates)
    row = next(r for r in candidates if r["rank"] == 1)
    if changes.get("drop"):
        del row["estimate"]["terms"][name]
    else:
        row["estimate"]["terms"][name].update(changes)
    return dataclasses.replace(s, candidates=candidates)


@pytest.mark.parametrize(
    ("name", "changes", "reason"),
    [
        ("output", {"drop": True}, "missing_term"),
        ("output", {"seconds": 0.0}, "raw_not_positive"),
        ("output", {"seconds": 0.01}, "below_min_predicted"),
        ("env", {"detail": {"env_cached": True}}, "exact_by_construction"),
    ],
)
def test_term_reasons(name: str, changes: dict[str, Any], reason: str) -> None:
    s = _edit_term(stored(pinned="node_001"), name, **changes)
    rows, skipped = observations(whole_run(s, phases=PHASES))
    assert name not in {o.term for o in rows}
    assert len(rows) == 3
    assert _skips(skipped) == {reason: 1}


@pytest.mark.parametrize(
    "kw",
    [
        {"node_id": "node_002"},  # ran elsewhere than chosen
        {"role": "combine"},
        {"state": "failed"},
    ],
)
def test_runs_the_matching_refuses_are_unmatched(kw: dict[str, Any]) -> None:
    s = stored(pinned="node_001")
    rows, skipped = observations(whole_run(s, phases=PHASES, **kw))
    assert rows == []
    assert _skips(skipped) == {"unmatched": 1}


def test_no_chosen_row_is_unmatched() -> None:
    s = stored(pinned="node_001")
    s = dataclasses.replace(s, chosen_node_id="node_002")  # rank 1 is node_001
    run = whole_run(s, phases=PHASES, node_id="node_002", role="run")
    rows, skipped = observations(run)
    assert rows == [] and _skips(skipped) == {"unmatched": 1}


def test_every_phase_skipped_names_each_term_not_one_unmatched() -> None:
    s = stored(pinned="node_001")
    phases = dict.fromkeys(PHASES, None)
    rows, skipped = observations(whole_run(s, phases=phases))
    assert rows == []
    assert _skips(skipped) == {"skipped_phase": 4}


# ------------------------------------------------------------------ Mode 3


def test_mode3_set_lanes_chunk_rates_and_combine() -> None:
    s = stored(forced_set=True)
    run = set_run(s)
    rows, skipped = observations(run)
    by: dict[tuple[str, str], Observation] = {(o.term, o.node_id): o for o in rows}
    nodes = chosen(s)["node_set"]
    assert nodes == ["node_000", "node_002", "node_001"]
    # lane_stage: node_000 holds every tree (0 s measured) -> excluded
    assert ("lane_stage", "node_000") not in by
    for node in ("node_002", "node_001"):
        stage = lane_row(s, node)["estimate"]["terms"]["transfer"]
        obs = by[("lane_stage", node)]
        assert obs.raw_s == stage["seconds"] == 5.0
        assert obs.y == math.log(2.0 / 5.0)
        # D366 (FEATURE_VERSION 2): a set's lane is the one-lane candidate's
        # shape (what serving computes), not the set's 3 lanes x 166 items
        one = lane_row(s, node)
        assert obs.lanes == 1 and obs.chunk_items == one["chunk_items"] != 166
    for i, node in enumerate(nodes):
        for name in ("lane_env", "chunk_rate"):
            shaped = by[(name, node)]
            assert (shaped.lanes, shaped.chunk_items) == (
                1,
                lane_row(s, node)["chunk_items"],
            )
        env = by[("lane_env", node)]
        assert env.raw_s == 30.0 and env.y == math.log(1.0 / 30.0)
        assert env.attempt_id == f"lane_{i}"
        rate = by[("chunk_rate", node)]
        planned = lane_row(s, node)["rate_items_s"]
        assert rate.raw_s == 1.0 / planned  # seconds per item
        assert rate.actual_s == (5.0 + 7.0) / 200  # committed chunks only
        assert rate.y == pytest.approx(math.log(0.06 * planned), rel=1e-12)
        assert rate.source == lane_row(s, node)["rate_source"]
    combine = by[("combine", "node_000")]
    raw = chosen(s)["estimate"]["terms"]["combine"]["seconds"]
    assert combine.raw_s == raw
    assert combine.actual_s == 2.0  # stage 0.4 + env skipped (0) + run 1.6
    assert combine.attempt_id == "combine_1"
    assert (combine.lanes, combine.chunk_items) == (3, 166)  # the set's own
    assert len(rows) == 2 + 3 + 3 + 1
    assert _skips(skipped) == {"exact_by_construction": 1}


def test_chunk_rate_arithmetic_ignores_uncommitted_and_unmeasured_chunks() -> None:
    s = stored(forced_set=True)
    chunks = {
        "node_001": (
            ChunkOutcome(items=50, run_ms=2_000, outcome="committed"),
            ChunkOutcome(items=30, run_ms=None, outcome="committed"),  # no run time
            ChunkOutcome(items=70, run_ms=4_000, outcome="committed"),
            ChunkOutcome(items=99, run_ms=1, outcome="superseded"),
            ChunkOutcome(items=99, run_ms=1, outcome="failed"),
        )
    }
    rows, _ = observations(set_run(s, chunks=chunks))
    rate = next(o for o in rows if o.term == "chunk_rate" and o.node_id == "node_001")
    assert rate.actual_s == 6.0 / 120


def test_a_lane_without_committed_chunks_counts_no_chunks() -> None:
    s = stored(forced_set=True)
    chunks = {"node_002": (ChunkOutcome(items=10, run_ms=100, outcome="lost"),)}
    rows, skipped = observations(set_run(s, chunks=chunks))
    assert ("chunk_rate", "node_002") not in {(o.term, o.node_id) for o in rows}
    assert _skips(skipped) == {"exact_by_construction": 1, "no_chunks": 1}


def test_a_tiny_lane_rate_prediction_is_below_the_floor() -> None:
    s = stored(forced_set=True)
    candidates = copy.deepcopy(s.candidates)
    for row in candidates:
        if row["mode"] != "split" and row["node_id"] == "node_001":
            row["rate_items_s"] = 10_000.0  # 10 items -> 0.001 s predicted
    s = dataclasses.replace(s, candidates=candidates)
    chunks = {"node_001": (ChunkOutcome(items=10, run_ms=100, outcome="committed"),)}
    _, skipped = observations(set_run(s, chunks=chunks))
    assert _skips(skipped) == {"exact_by_construction": 1, "below_min_predicted": 1}


def test_lane_not_done_and_missing_lane_row_skip_its_three_terms() -> None:
    s = stored(forced_set=True)
    rows, skipped = observations(set_run(s, lane_states={"node_001": "failed"}))
    assert all(o.node_id != "node_001" for o in rows)
    assert _skips(skipped) == {"exact_by_construction": 1, "lane_not_done": 3}
    candidates = [
        r
        for r in s.candidates
        if not (r["mode"] != "split" and r["node_id"] == "node_002")
    ]
    gone = dataclasses.replace(s, candidates=candidates)
    rows, skipped = observations(set_run(gone))
    assert all(o.node_id != "node_002" for o in rows)
    assert _skips(skipped) == {"exact_by_construction": 1, "missing_lane_row": 3}


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"combine_node": "node_001"}, "node_mismatch"),
        ({"combine_phases": {"stage": 1, "env": 1, "run": None}}, "skipped_phase"),
        ({"combine_phases": {"stage": 1, "run": 5}}, "missing_phase"),
        ({"combine_phases": {"stage": 1, "env": 1}}, "missing_phase"),
    ],
)
def test_combine_reasons(kw: dict[str, Any], reason: str) -> None:
    s = stored(forced_set=True)
    rows, skipped = observations(set_run(s, **kw))
    assert "combine" not in {o.term for o in rows}
    assert _skips(skipped) == {"exact_by_construction": 1, reason: 1}


@pytest.mark.parametrize(
    ("phases", "actual_s"),
    [
        # D346: stage (the other lanes' parts fetched) + env + run (process
        # start and the combine itself); collect is the job's `output` term
        ({"stage": 400, "env": 100, "run": 1500, "collect": 30}, 2.0),
        ({"stage": 400, "env": 100, "run": 1500, "collect": 99_999}, 2.0),
        ({"stage": 1500, "env": None, "run": 500}, 2.0),  # skipped env = 0 s
        ({"stage": 0, "env": 0, "run": 2000}, 2.0),  # nothing staged: still run
    ],
)
def test_combine_actual_is_stage_env_run_never_collect(
    phases: dict[str, int | None], actual_s: float
) -> None:
    s = stored(forced_set=True)
    raw = chosen(s)["estimate"]["terms"]["combine"]["seconds"]
    rows, _ = observations(set_run(s, combine_phases=phases))
    combine = next(o for o in rows if o.term == "combine")
    assert combine.actual_s == actual_s
    assert combine.y == math.log(actual_s / raw)


def test_no_combine_attempt_gives_no_combine_row_and_no_skip() -> None:
    s = stored(forced_set=True)
    run = dataclasses.replace(set_run(s), combine=None)
    rows, skipped = observations(run)
    assert "combine" not in {o.term for o in rows}
    assert _skips(skipped) == {"exact_by_construction": 1}


def test_a_set_whose_lead_lane_is_not_done_is_unmatched() -> None:
    s = stored(forced_set=True)
    run = set_run(s)
    run = dataclasses.replace(
        run, outcome=dataclasses.replace(run.outcome, attempt_state="failed")
    )
    rows, skipped = observations(run)
    assert rows == [] and _skips(skipped) == {"unmatched": 1}


def test_one_lane_split_learns_lane_terms_rate_and_combine() -> None:
    s = stored(split=True)
    candidates = copy.deepcopy(s.candidates)
    for row in candidates:  # make node_002's one-lane row the choice
        lone = row["mode"] != "split" and row["node_id"] == "node_002"
        row["rank"] = 1 if lone else (row["rank"] or 0) + 10
    s = dataclasses.replace(s, candidates=candidates, chosen_node_id="node_002")
    row = chosen(s)
    assert row["node_set"] == ["node_002"]
    lane = LaneOutcome(
        attempt_id="att_1",
        node_id="node_002",
        state="done",
        phases_ms=PHASES,
        finished_at=None,
        chunks=(ChunkOutcome(items=2000, run_ms=100_000, outcome="committed"),),
    )
    combine = CombineOutcome(
        attempt_id="comb",
        node_id=row["estimate"]["terms"]["combine"]["detail"]["combine_node_id"],
        state="done",
        phases_ms={"stage": 100, "env": 100, "run": 1000},
        finished_at=dt.datetime(2026, 10, 22, tzinfo=dt.UTC),
    )
    run = whole_run(s, phases=PHASES, lanes=(lane,), combine=combine)
    rows, skipped = observations(run)
    got = _by_term(rows)
    assert set(got) == {"lane_stage", "lane_env", "chunk_rate", "combine"}
    assert got["lane_stage"].raw_s == row["estimate"]["terms"]["transfer"]["seconds"]
    assert got["lane_stage"].y == math.log(12.0 / got["lane_stage"].raw_s)
    assert got["chunk_rate"].actual_s == 0.05
    assert got["chunk_rate"].raw_s == 1.0 / row["rate_items_s"]
    assert got["chunk_rate"].finished_at == run.outcome.finished_at  # lane had none
    assert got["combine"].actual_s == 1.2
    assert _skips(skipped) == {}


def test_combine_without_predicted_node_is_node_unknown() -> None:
    """Reviewer item 4: a combine term with no ``combine_node_id`` cannot be
    matched to the node that ran it, so it is a counted skip, never a row."""
    s = stored(forced_set=True)
    del chosen(s)["estimate"]["terms"]["combine"]["detail"]["combine_node_id"]
    rows, skipped = observations(set_run(s, combine_node="node_000"))
    assert "combine" not in {o.term for o in rows}
    assert _skips(skipped) == {"exact_by_construction": 1, "node_unknown": 1}
