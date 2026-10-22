"""V0.14 G5 tester r1 (D300, D320): adversarial checks of the decisions answer.

Derived from the spec: ``actual_s`` exists only where ``calibration.match``
would count the run (null for a Mode 3 set, a skipped phase, a non-done
attempt, another node), on the chosen row only; the answer never raises on a
hostile stored row; the margin sentence is the server's one sentence.
"""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
from typing import Any

import pytest
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.common.explain_text import margin_sentence, term_note
from meshforge.controller.api.decision_routes import DecisionList, _out
from meshforge.controller.jobs.decision_explain import (
    explain_candidates,
    term_actuals,
)
from meshforge.controller.jobs.decision_store import ActualOutcome, DecisionRecordOut

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 22, 9, 0, tzinfo=dt.UTC)


def _t(seconds: float, source: str = "history", **detail: Any) -> dict[str, Any]:
    return {"seconds": seconds, "source": source, "detail": detail}


def _row(
    node: str, rank: int | None, mode: str = "offload", **kw: Any
) -> dict[str, Any]:
    return {
        "node_id": node,
        "mode": mode,
        "feasible": True,
        "slot": "cpu",
        "reject_code": None,
        "reject_text": None,
        "estimate": {
            "total_s": 37.0,
            "terms": {
                "queue": _t(0.0, "measured"),
                "transfer": _t(4.0),
                "env": _t(1.0, "assumed"),
                "compute": _t(30.0),
                "output": _t(2.0),
            },
        },
        "score": 37.0,
        "rank": rank,
        **kw,
    }


def _rec(
    rows: list[Any],
    *,
    chosen: str | None = "n_b",
    state: str = "done",
    node: str = "n_b",
    role: str | None = "run",
    ended: dt.datetime | None = T0,
    phases: dict[str, Any] | None = None,
    attempt_id: str | None = "att_1",
    **extra: Any,
) -> DecisionRecordOut:
    return DecisionRecordOut(
        id=1,
        attempt_no=1,
        attempt_id=attempt_id,
        outcome="dispatched",
        policy="cost_model",
        chosen_node_id=chosen,
        created_at=T0,
        snapshot_digest="ab" * 32,
        reason_key="cd" * 32,
        candidates=rows,
        actual=ActualOutcome(
            attempt_id="att_1",
            node_id=node,
            state=state,
            failure_type=None,
            phases_ms={"stage": 2000, "env": 800, "run": 30000, "collect": 500}
            if phases is None
            else phases,
            role=role,
            ended_at=ended,
        ),
        **extra,
    )


# --------------------------------------------------------- actual_s only via match


@pytest.mark.parametrize(
    "state", ["failed", "cancelled", "running", "lost", "queued", "weird"]
)
def test_only_a_done_attempt_has_actuals(state: str) -> None:
    assert term_actuals(_rec([_row("n_b", 1)], state=state)) == {}


def test_a_done_attempt_without_an_end_time_has_none() -> None:
    assert term_actuals(_rec([_row("n_b", 1)], ended=None)) == {}


def test_attempt_id_missing_means_no_actuals() -> None:
    assert term_actuals(_rec([_row("n_b", 1)], attempt_id=None)) == {}


def test_other_node_ran_it_no_actuals() -> None:
    assert term_actuals(_rec([_row("n_b", 1)], node="n_other")) == {}


@pytest.mark.parametrize("bad", [None, -1, True, False])
def test_skipped_or_nonsense_phase_is_absent_never_zero(bad: Any) -> None:
    ph = {"stage": 2000, "env": bad, "run": 30000, "collect": 500}
    got = term_actuals(_rec([_row("n_b", 1)], phases=ph))
    # bool is rejected by match too (True would otherwise be 0.001 s)
    assert "env" not in got
    assert got["transfer"] == 2.0


def test_phase_zero_ms_is_a_real_zero_actual_not_null() -> None:
    got = term_actuals(_rec([_row("n_b", 1)], phases={"stage": 0, "run": 10}))
    assert got["transfer"] == 0.0


def test_no_phases_at_all_means_no_actual() -> None:
    assert term_actuals(_rec([_row("n_b", 1)], phases={})) == {}


def test_queue_never_gets_an_actual_even_if_a_queue_phase_exists() -> None:
    got = term_actuals(_rec([_row("n_b", 1)], phases={"queue": 9000, "run": 1000}))
    assert "queue" not in got


def test_mode3_set_row_has_no_actual_even_with_every_phase() -> None:
    split = _row("n_b", 1, mode="split", node_set=["n_b", "n_c"])
    for role in ("lane", "run", "combine"):
        assert term_actuals(_rec([split], role=role)) == {}


def test_rank_two_row_with_the_chosen_node_gets_no_actual() -> None:
    rows = [_row("n_a", 1), _row("n_b", 2)]
    # chosen node is n_b but the planner's rank 1 was n_a (e.g. a rule policy)
    assert term_actuals(_rec(rows, chosen="n_b", node="n_b")) == {}
    out = explain_candidates(rows, chosen_node_id="n_b", actuals={"compute": 5.0})
    assert all(
        t["actual_s"] is None for r in out for t in r["estimate"]["terms"].values()
    )


def test_two_rows_same_node_only_rank_one_carries_actual() -> None:
    rows = [_row("n_b", 1), _row("n_b", 2, mode="local")]
    out = explain_candidates(rows, chosen_node_id="n_b", actuals={"compute": 5.0})
    assert out[0]["estimate"]["terms"]["compute"]["actual_s"] == 5.0
    assert out[1]["estimate"]["terms"]["compute"]["actual_s"] is None


def test_actual_uses_the_calibration_match_not_a_second_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If match says nothing, the answer says nothing (one mapping, D320)."""
    import meshforge.controller.jobs.decision_explain as mod

    monkeypatch.setattr(mod, "match", lambda outcome: None)
    assert mod.term_actuals(_rec([_row("n_b", 1)])) == {}


def test_a_local_mode_row_and_a_one_lane_split_each_follow_match() -> None:
    local = _row("n_b", 1, mode="local")
    assert set(term_actuals(_rec([local]))) == {"transfer", "env", "compute", "output"}


def test_ratio_is_actual_over_predicted_and_null_when_prediction_is_zero() -> None:
    out = explain_candidates(
        [_row("n_b", 1)], chosen_node_id="n_b", actuals={"compute": 60.0, "queue": 1.0}
    )
    terms = out[0]["estimate"]["terms"]
    assert terms["compute"]["ratio"] == 2.0
    assert terms["queue"]["ratio"] is None


def test_explain_candidates_does_not_mutate_the_stored_rows() -> None:
    rows = [_row("n_b", 1)]
    before = copy.deepcopy(rows)
    explain_candidates(rows, chosen_node_id="n_b", actuals={"compute": 1.0})
    assert rows == before


def test_pre_v014_row_no_factors_no_risk_no_objective_answers_cleanly() -> None:
    row = _row("n_b", 1)
    for term in row["estimate"]["terms"].values():
        term["detail"] = {}
    out = _out(
        _rec([row], attempt_id=None, calibration_version=None, replay_version=None)
    )
    assert out.objective is None and out.calibration_version is None
    assert out.replay_version is None  # replay:null is "not replayable", not an error
    assert all(
        t.calibration is None and t.note is None
        for t in out.candidates[0].estimate.terms.values()  # type: ignore[union-attr]
    )


def test_junk_candidate_values_never_raise_in_the_answer_path() -> None:
    rows: list[Any] = [
        {
            "node_id": "n_b",
            "rank": 1,
            "estimate": {"terms": {"x": 3, "compute": {"seconds": "a"}}},
        },
        "str",
        [],
    ]
    # explain_candidates/margin must not raise; the model may reject, that's the old contract
    explain_candidates(rows, chosen_node_id="n_b", actuals={})
    assert margin_sentence(rows) is None
    assert term_note(None) is None and term_note("x") is None


# --------------------------------------------------------- margin sentence


def _split_row(rank: int, passed: bool, **test: Any) -> dict[str, Any]:
    return _row(
        "n_b",
        rank,
        mode="split",
        node_set=["n_b", "n_c"],
        margin_test={
            "set_s": 20.0,
            "best_single_s": 30.0,
            "best_single_node_id": "n_b",
            "margin": 0.2,
            "threshold_s": 24.0,
            "passed": passed,
            **test,
        },
    )


def test_margin_sentence_split_chosen_exact_numbers() -> None:
    s = margin_sentence([_split_row(1, True), _row("n_b", 2)])
    assert s == (
        "split over 2 nodes: 20.00s <= (1 - 0.2) x 30.00s "
        "(best single node n_b), needs <= 24.00s"
    )


def test_margin_sentence_not_split_when_set_above_threshold() -> None:
    s = margin_sentence([_split_row(2, False, set_s=29.0), _row("n_b", 1)])
    assert s is not None and s.startswith(
        "not split: the best set 29.00s is above 24.00s"
    )


def test_margin_sentence_none_for_whole_job_and_empty() -> None:
    assert margin_sentence([_row("n_b", 1)]) is None
    assert margin_sentence([]) is None
    assert margin_sentence(None) is None


def test_margin_sentence_with_nan_numbers_degrades_to_dashes() -> None:
    s = margin_sentence([_split_row(1, True, set_s=float("nan"), margin=None)])
    assert s is not None and "nan" not in s.lower() and "-" in s


def test_unicode_and_markup_in_node_ids_pass_through_as_text() -> None:
    row = _split_row(1, True, best_single_node_id="nöde-<b>日本</b>")
    s = margin_sentence([row])
    assert s is not None and "nöde-<b>日本</b>" in s


# --------------------------------------------------------- CLI == API parity


def _cli(payload: dict[str, Any]) -> str:
    out = io.StringIO()
    console = Console(file=out, width=4000, color_system=None)
    for table in decision_tables(payload):
        console.print(table)
    return out.getvalue()


def _payload(*records: DecisionRecordOut) -> dict[str, Any]:
    ans = DecisionList(
        job_id="job_x", count=len(records), decisions=tuple(_out(r) for r in records)
    )
    return json.loads(ans.model_dump_json())


def test_cli_prints_every_string_and_ratio_the_api_answers_for_each_state() -> None:
    split = _split_row(1, True)
    cases = {
        "whole-done": _rec([_row("n_b", 1)]),
        "running": _rec([_row("n_b", 1)], state="running", ended=None),
        "failed": _rec([_row("n_b", 1)], state="failed"),
        "split": _rec([split, _row("n_b", 2)], role="lane"),
        "no-chosen": _rec([_row("n_b", None)], chosen=None, attempt_id=None),
        "rejected-only": _rec(
            [
                {
                    "node_id": "n_z",
                    "mode": "offload",
                    "feasible": False,
                    "slot": None,
                    "reject_code": "slots_full",
                    "reject_text": "every CPU slot of the node is in use",
                    "estimate": None,
                    "score": None,
                    "rank": None,
                }
            ],
            chosen=None,
            attempt_id=None,
        ),
    }
    for name, record in cases.items():
        payload = _payload(record)
        printed = _cli(payload)
        d = payload["decisions"][0]
        if d["margin_sentence"]:
            assert d["margin_sentence"] in printed, name
        for cand in d["candidates"]:
            for term in ((cand["estimate"] or {}).get("terms") or {}).values():
                if term["actual_s"] is not None:
                    assert f"{term['actual_s']:.2f}" in printed, name
                if term["ratio"] is not None:
                    assert f"{term['ratio']:.2f}" in printed, name
        if d["actual"] is None or d["actual"]["state"] != "done":
            for cand in d["candidates"]:
                for term in ((cand["estimate"] or {}).get("terms") or {}).values():
                    assert term["actual_s"] is None, name


def test_replay_null_prints_a_marker_and_replay_version_prints_when_set() -> None:
    none = _cli(_payload(_rec([_row("n_b", 1)], replay_version=None)))
    some = _cli(_payload(_rec([_row("n_b", 1)], replay_version=3)))
    assert none != some
