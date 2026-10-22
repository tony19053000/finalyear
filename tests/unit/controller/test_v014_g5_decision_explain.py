"""V0.14 G5 (D300, D320): what the decisions answer derives from a stored
decision - per term ``calibration`` / ``note`` / ``actual_s`` / ``ratio``, per
decision ``calibration_version`` / ``replay_version`` / ``margin_sentence``.

``actual_s`` must come from the calibration's own match (one term -> phase
mapping): every case where ``match`` refuses a run is a case where the answer
has no actual, never a 0 or a guessed phase.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pytest
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.api.decision_routes import DecisionOut, _out
from meshforge.controller.jobs.decision_explain import (
    explain_candidates,
    term_actuals,
)
from meshforge.controller.jobs.decision_store import ActualOutcome, DecisionRecordOut
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    CALIBRATED_TERMS,
    Calibration,
    Factor,
    TermFactors,
)
from meshforge.controller.planner.decision_json import candidates_json

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 22, 9, 0, tzinfo=dt.UTC)
PHASES = {"queue": 50, "stage": 2_000, "env": None, "run": 30_000, "collect": 500}


def _term(seconds: float, source: str = "history", **detail: Any) -> dict[str, Any]:
    return {"seconds": seconds, "source": source, "detail": detail}


def _whole_rows() -> list[dict[str, Any]]:
    chosen = {
        "node_id": "node_b",
        "mode": "offload",
        "feasible": True,
        "slot": "cpu",
        "reject_code": None,
        "reject_text": None,
        "estimate": {
            "total_s": 37.0,
            "terms": {
                "queue": _term(0.0, "measured"),
                "transfer": _term(
                    4.0,
                    bw_from="controller",
                    calibration={
                        "factor": 2.0,
                        "n": 14,
                        "key_level": "node",
                        "version": 7,
                        "raw_s": 2.0,
                    },
                ),
                "env": _term(1.0, "assumed"),
                "compute": _term(30.0),
                "output": _term(2.0),
            },
        },
        "score": 37.0,
        "rank": 1,
    }
    other = {
        **chosen,
        "node_id": "node_a",
        "mode": "local",
        "rank": 2,
        "score": 50.0,
    }
    rejected = {
        "node_id": "node_c",
        "mode": "offload",
        "feasible": False,
        "slot": None,
        "reject_code": "slots_full",
        "reject_text": "every CPU slot of the node is in use",
        "estimate": None,
        "score": None,
        "rank": None,
    }
    return [chosen, other, rejected]


def _record(
    rows: list[dict[str, Any]],
    *,
    state: str = "done",
    node_id: str = "node_b",
    role: str | None = "run",
    ended_at: dt.datetime | None = T0,
    phases: dict[str, int | None] | None = None,
    chosen: str | None = "node_b",
    **extra: Any,
) -> DecisionRecordOut:
    return DecisionRecordOut(
        id=11,
        attempt_no=1,
        attempt_id="att_1",
        outcome="dispatched",
        policy="cost_model",
        chosen_node_id=chosen,
        created_at=T0,
        snapshot_digest="ab" * 32,
        reason_key="cd" * 32,
        candidates=rows,
        actual=ActualOutcome(
            attempt_id="att_1",
            node_id=node_id,
            state=state,
            failure_type=None,
            phases_ms=PHASES if phases is None else phases,
            role=role,
            ended_at=ended_at,
        ),
        **extra,
    )


# ------------------------------------------------------------------ actuals


def test_a_done_whole_job_gets_its_terms_actual_from_the_calibration_match() -> None:
    actuals = term_actuals(_record(_whole_rows()))
    # stage -> transfer, run -> compute, collect -> output; env skipped: absent;
    # queue is never a calibrated term (always 0 s measured)
    assert actuals == {"transfer": 2.0, "compute": 30.0, "output": 0.5}


@pytest.mark.parametrize(
    "change",
    [
        {"state": "failed"},
        {"state": "running", "ended_at": None},
        {"node_id": "node_a"},  # ran elsewhere than the decision chose
        {"role": "lane"},  # a whole job's decision must be role run
        {"role": None},
        {"ended_at": None},
        {"chosen": None},
    ],
)
def test_no_actual_where_match_refuses_the_run(change: dict[str, Any]) -> None:
    assert term_actuals(_record(_whole_rows(), **change)) == {}


def test_no_record_actual_means_no_term_actual() -> None:
    record = dataclasses.replace(_record(_whole_rows()), actual=None)
    assert term_actuals(record) == {}


def test_a_mode3_set_has_no_per_term_actual_and_a_one_lane_split_only_stage_env() -> (
    None
):
    lane = _whole_rows()[0] | {"node_set": ["node_b"], "chunk_items": 5, "chunks": 4}
    split_set = lane | {
        "node_id": "node_b",
        "mode": "split",
        "node_set": ["node_b", "node_a"],
    }
    assert term_actuals(_record([split_set], role="lane")) == {}
    # D269 "don't split": one lane, role lane - only transfer (stage) and env
    one = term_actuals(_record([lane], role="lane", phases={**PHASES, "env": 1_250}))
    assert one == {"transfer": 2.0, "env": 1.25}
    assert term_actuals(_record([lane], role="run")) == {}


def test_explain_candidates_puts_actual_and_ratio_on_the_chosen_row_only() -> None:
    rows = explain_candidates(
        _whole_rows(),
        chosen_node_id="node_b",
        actuals={"transfer": 2.0, "compute": 45.0},
    )
    chosen, other, rejected = rows
    terms = chosen["estimate"]["terms"]
    assert terms["transfer"]["actual_s"] == 2.0
    assert terms["transfer"]["ratio"] == 0.5  # 2 s measured vs 4 s predicted
    assert terms["compute"]["ratio"] == 1.5
    assert terms["env"]["actual_s"] is None and terms["env"]["ratio"] is None
    assert terms["queue"]["ratio"] is None  # 0 s predicted: no ratio
    assert all(t["actual_s"] is None for t in other["estimate"]["terms"].values())
    assert rejected == _whole_rows()[2]  # no estimate: passed on unchanged


def test_a_zero_second_prediction_with_an_actual_has_no_ratio() -> None:
    rows = explain_candidates(
        _whole_rows(), chosen_node_id="node_b", actuals={"queue": 3.0}
    )
    queue = rows[0]["estimate"]["terms"]["queue"]
    assert queue["actual_s"] == 3.0 and queue["ratio"] is None


def test_the_calibration_block_is_lifted_and_the_note_is_the_shared_text() -> None:
    rows = explain_candidates(_whole_rows(), chosen_node_id="node_b", actuals={})
    transfer = rows[0]["estimate"]["terms"]["transfer"]
    assert transfer["calibration"] == {
        "factor": 2.0,
        "n": 14,
        "key_level": "node",
        "version": 7,
        "raw_s": 2.0,
    }
    assert transfer["note"] == "from controller"
    assert rows[0]["estimate"]["terms"]["env"]["calibration"] is None
    assert rows[0]["estimate"]["terms"]["env"]["note"] is None


@pytest.mark.parametrize(
    "block",
    [
        "x",
        {"factor": "2"},
        {"factor": True},
        {"factor": float("nan")},
        {"n": 3},
    ],
)
def test_an_unreadable_calibration_block_is_no_calibration(block: Any) -> None:
    rows = _whole_rows()
    rows[0]["estimate"]["terms"]["compute"]["detail"] = {"calibration": block}
    out = explain_candidates(rows, chosen_node_id="node_b", actuals={})
    assert out[0]["estimate"]["terms"]["compute"]["calibration"] is None


def test_hostile_rows_pass_through_without_raising() -> None:
    rows: list[Any] = [3, {"estimate": "x"}, {"estimate": {"terms": 4}}, None]
    assert explain_candidates(rows, chosen_node_id=None, actuals={}) == rows


# ------------------------------------------------------------------ answer


def test_the_answer_carries_calibration_replay_and_margin_fields() -> None:
    out = _out(_record(_whole_rows(), calibration_version=7, replay_version=2))
    assert isinstance(out, DecisionOut)
    assert out.calibration_version == 7 and out.replay_version == 2
    assert out.margin_sentence is None  # a whole job
    transfer = out.candidates[0].estimate.terms["transfer"]  # type: ignore[union-attr]
    assert transfer.calibration is not None and transfer.calibration.factor == 2.0
    assert transfer.actual_s == 2.0 and transfer.ratio == 0.5
    assert transfer.note == "from controller"


def test_a_pre_v014_row_answers_nulls_not_errors() -> None:
    rows = _whole_rows()
    for row in rows:
        estimate = row.get("estimate")
        for term in (estimate or {}).get("terms", {}).values():
            term["detail"] = {}
    record = dataclasses.replace(
        _record(rows), actual=None, attempt_id=None, outcome="waiting"
    )
    out = _out(record)
    assert out.calibration_version is None and out.replay_version is None
    assert out.objective is None and out.margin_sentence is None
    for candidate in out.candidates:
        for term in (candidate.estimate.terms if candidate.estimate else {}).values():
            assert term.calibration is None and term.actual_s is None


def test_a_real_calibrated_split_decision_round_trips_through_the_answer() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=2)
    calibration = Calibration(
        version=5,
        terms={t: TermFactors(global_=Factor(1.5, 9, 1.6)) for t in CALIBRATED_TERMS},
    )
    decision = plan(request, snapshot, "cost_model", now, calibration=calibration)
    record = dataclasses.replace(
        _record(
            candidates_json(decision),
            chosen=decision.chosen.node_id if decision.chosen else None,
        ),
        actual=None,
        calibration_version=5,
    )
    out = _out(record)
    assert out.margin_sentence is not None
    assert out.margin_sentence.startswith(("split over ", "not split: "))
    scaled = [
        term.calibration
        for c in out.candidates
        if c.estimate is not None
        for term in c.estimate.terms.values()
        if term.calibration is not None
    ]
    assert scaled and all(c.factor == 1.5 and c.version == 5 for c in scaled)
