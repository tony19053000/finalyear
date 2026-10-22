"""V0.14 G4 (D297, D313, D315, D317): the evaluator core on hand-built campaigns
where the right answer is known.

The fabric predicts ``node_b`` fastest (history), so ``cost_model`` picks
``mode1`` on ``node_b``; every rule policy but ``random`` picks ``node_a``
(Local) on this tidy fabric (lowest id, equal loads/scores, the only cached
tree). The measured medians then decide each pick's regret:

* ``best`` group, cost_model picks the best -> regret 0; a rule policy's pick
  3x slower -> regret 200 %, ``median diff`` ms;
* ``tie`` group: a pick in the tie set -> 0, outside it -> vs the lowest median;
* ``insufficient`` / ``pending`` -> excluded, reported with the reason, never
  counted; no replay / unreadable / forced without shadow -> skipped records,
  a group with none -> ``no_replay``;
* a pick no configuration measured -> ``unmeasured`` (never 0);
* Mode 3 sets and D269 "don't split" map to their configurations;
* ``random`` / ``round_robin`` reproduce the planner on the stored replay;
* the group pick is the majority; ``unmeasured`` wins a tie it is in;
* the report is deterministic and independent of input order.
"""

from __future__ import annotations

import dataclasses
import random
from collections import Counter

import pytest
from mf_eval_world import NOW, ORIGIN, config_id, fabric, group, records

from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import shadow_from_replay
from meshforge.controller.planner_eval.core import (
    evaluate,
    group_regret,
    planned_inputs,
    vote,
)
from meshforge.controller.planner_eval.mapping import config_for, pick_of
from meshforge.controller.planner_eval.model import (
    CRITERION_POLICY,
    ORACLE,
    UNMEASURED,
    EvalConfig,
    PolicySpec,
    default_policies,
)
from meshforge.controller.planner_eval.report import report_text

pytestmark = pytest.mark.unit

FAST_B = {"node_a": 100.0, "node_b": 10.0, "node_c": 50.0, "node_d": 80.0}
CFG = EvalConfig(bootstrap_resamples=200)
COST = "cost_model:uncalibrated:min_time"


def _run(groups, recs, policies=None, **kw):  # type: ignore[no-untyped-def]
    return evaluate(
        groups,
        recs,
        {},
        policies or default_policies(),
        kw.pop("config", CFG),
        campaign_ids=["camp_1"],
        **kw,
    )


def _picks(report, group_id: str) -> dict[str, dict]:  # type: ignore[type-arg]
    (g,) = [g for g in report["groups"] if g["group_id"] == group_id]
    return {p["key"]: p for p in g["picks"]}


def test_best_group_regret_zero_for_the_best_and_relative_for_others() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200, "node_d": 250})
    report = _run([g], records(g, request, snapshot))
    picks = _picks(report, "g1")
    assert picks[COST]["config_id"] == config_id("g1", "mode1", "node_b")
    assert (picks[COST]["regret_pct"], picks[COST]["regret_ms"]) == (0.0, 0.0)
    rr = picks["round_robin"]
    assert rr["config_id"] == config_id("g1", "local", ORIGIN)
    # (300 - 100) / 100: slower is a POSITIVE regret (regret-sign mutation)
    assert (rr["regret_pct"], rr["regret_ms"]) == (200.0, 200.0)
    assert picks[ORACLE]["config_id"] == g.best_config_id
    assert picks[ORACLE]["regret_pct"] == 0.0
    for pick in picks.values():
        assert pick["status"] == "counted"
        assert pick["regret_pct"] >= 0.0


def test_tie_set_pick_is_zero_and_outside_the_tie_is_vs_the_lowest_median() -> None:
    request, snapshot = fabric(FAST_B)
    # node_b and node_a tied (CIs overlap); node_b has the lower median
    g = group(
        "g2",
        {"node_a": 105, "node_b": 100, "node_c": 300},
        label="tie",
        tied=("node_a", "node_b"),
    )
    report = _run([g], records(g, request, snapshot))
    picks = _picks(report, "g2")
    assert picks["round_robin"]["config_id"] == config_id("g2", "local", ORIGIN)
    # in the tie set: regret 0 although its median is 5 % higher (tie -> 0)
    assert picks["round_robin"]["regret_pct"] == 0.0
    assert picks[COST]["regret_pct"] == 0.0
    # a pick outside the tie set is compared with the lowest median
    assert group_regret(g, config_id("g2", "mode1", "node_c"), 100.0) == (
        200.0,
        200.0,
    )
    assert picks[ORACLE]["config_id"] == config_id("g2", "mode1", "node_b")


def test_insufficient_and_pending_are_excluded_and_reported() -> None:
    request, snapshot = fabric(FAST_B)
    short = group("g3", {"node_a": 1, "node_b": 2}, label="insufficient", reason="n<3")
    open_ = group("g4", {"node_a": 1, "node_b": 2}, label="pending", reason="1 run")
    report = _run(
        [short, open_],
        records(short, request, snapshot) + records(open_, request, snapshot),
    )
    by_id = {g["group_id"]: g for g in report["groups"]}
    assert by_id["g3"]["status"] == "excluded"
    assert by_id["g3"]["excluded_reason"] == "insufficient: n<3"
    assert by_id["g4"]["excluded_reason"] == "pending: 1 run"
    assert by_id["g3"]["picks"] == [] and by_id["g4"]["picks"] == []
    assert report["counts"]["labels"] == {
        "best": 0,
        "tie": 0,
        "insufficient": 1,
        "pending": 1,
    }
    assert report["counts"]["evaluated_groups"] == 0
    for row in report["policies"]:
        assert row["counted_groups"] == 0 and row["mean_regret_pct"] is None


def test_records_without_usable_replays_are_skipped_and_counted() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g5", {"node_a": 300, "node_b": 100})
    good = records(g, request, snapshot, reps=1)
    none = records(g, request, snapshot, reps=1, replay=None)
    broken = records(g, request, snapshot, reps=1, replay={"replay_version": 99})
    forced = records(g, request, snapshot, reps=1, shadow=False)
    mixed = [
        good[0],
        dataclasses.replace(none[1], attempt_id="x1"),
        dataclasses.replace(broken[0], attempt_id="x2"),
        dataclasses.replace(forced[1], attempt_id="x3"),
    ]
    report = _run([g], mixed)
    counts = report["counts"]
    assert counts["records"] == 4 and counts["records_used"] == 1
    assert counts["records_skipped"] == {
        "no_replay": 1,
        "replay_unreadable": 1,
        "no_shadow": 1,
        "replay_unplannable": 0,
    }
    assert _picks(report, "g5")[COST]["votes"] == {
        config_id("g5", "mode1", "node_b"): 1
    }


def test_a_group_without_any_usable_replay_is_no_replay_not_counted() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g6", {"node_a": 300, "node_b": 100})
    report = _run([g], records(g, request, snapshot, replay=None))
    (row,) = report["groups"]
    assert row["status"] == "no_replay" and row["picks"] == []
    assert report["counts"]["no_replay_groups"] == 1
    for policy in report["policies"]:
        assert policy["no_replay_groups"] == 1
        assert policy["counted_groups"] == 0


def test_a_pick_the_campaign_did_not_measure_is_unmeasured_never_zero() -> None:
    """cost_model picks node_b; the campaign measured only node_a and node_c."""
    request, snapshot = fabric(FAST_B)
    g = group("g7", {"node_a": 300, "node_c": 100})
    report = _run([g], records(g, request, snapshot))
    cost = _picks(report, "g7")[COST]
    assert cost["status"] == "unmeasured"
    assert cost["regret_pct"] is None and cost["config_id"] is None
    assert cost["votes"] == {UNMEASURED: 6}
    (row,) = [p for p in report["policies"] if p["key"] == COST]
    assert row["counted_groups"] == 0 and row["unmeasured_groups"] == 1
    assert row["mean_regret_pct"] is None  # never averaged in as 0


def test_unsupported_configuration_is_never_a_pick() -> None:
    request, snapshot = fabric(FAST_B)
    g = group(
        "g8", {"node_a": 300, "node_b": 100, "node_c": 90}, unsupported=("node_b",)
    )
    picks = _picks(_run([g], records(g, request, snapshot)), "g8")
    assert picks[COST]["status"] == "unmeasured"


def test_mode3_set_and_dont_split_groups() -> None:
    request, snapshot = fabric({}, split_items=4000)
    full = ("node_a", "node_b", "node_c", "node_d")
    good = group("s1", {"node_a": 900, "node_b": 800}, sets={full: 300})
    worse = group("s2", {"node_a": 900, "node_b": 200}, sets={full: 300})
    report = _run(
        [good, worse],
        records(good, request, snapshot) + records(worse, request, snapshot),
    )
    s1, s2 = _picks(report, "s1")[COST], _picks(report, "s2")[COST]
    assert s1["config_id"] == config_id("s1", "mode3", "node_a", full)
    assert s1["regret_pct"] == 0.0
    assert s2["config_id"] == config_id("s2", "mode3", "node_a", full)
    assert s2["regret_pct"] == 50.0  # (300 - 200) / 200
    # a fabric where splitting does not pay: "don't split" maps to mode1/local
    one, one_snap = fabric(FAST_B, split_items=2)
    decision = plan(one, one_snap, "cost_model", NOW)
    assert decision.split is not None and not decision.split.split
    g = group("s3", {"node_a": 300, "node_b": 100}, sets={full: 50})
    found = config_for(pick_of(decision), ORIGIN, g.configs)
    assert found is not None and found.strategy in ("local", "mode1")


def test_random_and_round_robin_reproduce_the_planner_on_the_stored_replay() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g9", {"node_a": 300, "node_b": 100, "node_c": 200, "node_d": 250})
    recs = records(g, request, snapshot, reps=5)
    report = _run([g], recs, [PolicySpec("random"), PolicySpec("round_robin")])
    for key in ("random", "round_robin"):
        expected: Counter[str] = Counter()
        for record in recs:
            assert record.replay is not None
            shadow = shadow_from_replay(record.replay)
            assert shadow is not None
            found = config_for(
                pick_of(plan(*shadow[:2], key, shadow[2])), ORIGIN, g.configs
            )
            expected[UNMEASURED if found is None else found.config_id] += 1
        assert _picks(report, "g9")[key]["votes"] == dict(sorted(expected.items()))
    # random really varies with the stored seed (job id + attempt number)
    assert len(_picks(report, "g9")["random"]["votes"]) > 1


def test_vote_majority_ties_and_unmeasured() -> None:
    assert vote({}) is None
    assert vote({"cfg_b": 3, "cfg_a": 1}) == "cfg_b"
    assert vote({"cfg_b": 2, "cfg_a": 2}) == "cfg_a"  # smallest config id
    assert vote({"cfg_b": 2, UNMEASURED: 2}) == UNMEASURED
    assert vote({"cfg_b": 3, UNMEASURED: 2}) == "cfg_b"


def test_group_pick_is_the_majority_of_its_records() -> None:
    """Half of the records see a fabric where node_c is predicted fastest."""
    request, snapshot = fabric(FAST_B)
    g = group("g10", {"node_a": 300, "node_b": 100, "node_c": 200})
    fast_c, snap_c = fabric({"node_a": 100, "node_b": 50, "node_c": 5})
    recs = records(g, request, snapshot, reps=2)  # 6 records -> node_b
    recs += [
        dataclasses.replace(r, attempt_id=f"c{i}")
        for i, r in enumerate(records(g, fast_c, snap_c, reps=1))  # 3 -> node_c
    ]
    pick = _picks(_run([g], recs), "g10")[COST]
    assert pick["votes"] == {
        config_id("g10", "mode1", "node_b"): 6,
        config_id("g10", "mode1", "node_c"): 3,
    }
    assert pick["config_id"] == config_id("g10", "mode1", "node_b")


def test_holdout_restricts_to_one_family() -> None:
    request, snapshot = fabric(FAST_B)
    a = group("ga", {"node_a": 300, "node_b": 100}, family="cpu.matmul")
    b = group("gb", {"node_a": 300, "node_b": 100}, family="cpu.sort")
    recs = records(a, request, snapshot) + records(b, request, snapshot)
    report = _run([a, b], recs, holdout="cpu.sort")
    assert [g["group_id"] for g in report["groups"]] == ["gb"]
    assert report["holdout"] == "cpu.sort"
    assert {f["family"] for f in report["families"]} == {"cpu.sort"}


def test_report_is_deterministic_and_independent_of_input_order() -> None:
    request, snapshot = fabric(FAST_B)
    groups = [
        group(f"g{i:02d}", {"node_a": 300 + i, "node_b": 100 + i, "node_c": 200})
        for i in range(6)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot)]
    first = report_text(_run(groups, recs))
    again = report_text(_run(groups, recs))
    shuffled_groups, shuffled_recs = list(groups), list(recs)
    rng = random.Random(7)  # noqa: S311 - test data
    rng.shuffle(shuffled_groups)
    rng.shuffle(shuffled_recs)
    assert first == again == report_text(_run(shuffled_groups, shuffled_recs))


def test_a_policy_row_listed_twice_is_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="twice"):
        _run([], [], [PolicySpec("random"), PolicySpec("random")])


def test_planned_inputs_prefers_the_shadow_and_refuses_a_pinned_request() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g11", {"node_a": 300, "node_b": 100})
    (with_shadow, *_rest) = records(g, request, snapshot, reps=1)
    inputs = planned_inputs(with_shadow)
    assert not isinstance(inputs, str)
    assert inputs[0].pinned_node_id is None and inputs[2] == NOW
    (forced, *_others) = records(g, request, snapshot, reps=1, shadow=False)
    assert planned_inputs(forced) == "no_shadow"


def test_criterion_policy_is_in_the_default_rows_and_oracle_is_last() -> None:
    keys = [p.key for p in default_policies()]
    assert CRITERION_POLICY.key in keys
    assert keys[-1] == ORACLE
    assert len(keys) == len(set(keys)) == 6 + 6 + 1


@pytest.mark.parametrize("odd", [float("nan"), float("inf")])
def test_a_non_finite_median_excludes_its_group_with_the_reason(odd: float) -> None:
    """G4 fix round 1 (T3): never planned, never NaN/Infinity in the JSON."""
    request, snapshot = fabric(FAST_B)
    mixed = group("g12", {"node_a": 300, "node_b": 100, "node_c": odd})
    fine = group("g13", {"node_a": 300, "node_b": 100})
    report = _run(
        [mixed, fine],
        records(mixed, request, snapshot) + records(fine, request, snapshot),
    )
    by_id = {g["group_id"]: g for g in report["groups"]}
    assert by_id["g12"]["status"] == "excluded"
    assert by_id["g12"]["excluded_reason"] == "non-finite median: " + config_id(
        "g12", "mode1", "node_c"
    )
    assert by_id["g13"]["status"] == "evaluated"
    assert report["counts"]["non_finite_configs"] == 1
    text = report_text(report)  # allow_nan=False: raises on NaN / Infinity
    assert "NaN" not in text and "Infinity" not in text
