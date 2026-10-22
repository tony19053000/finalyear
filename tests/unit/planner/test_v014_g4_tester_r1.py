"""V0.14 G4 tester round 1 - the offline evaluator, derived from brief 5d (D313-D319).

Independent of the implementer's tests: known-answer campaigns, vote ties,
regret relative + absolute, leave-one-family-out (spying on the fit's input),
the leak stamp, byte-identical reports, ``too_few_groups``, criterion 2 never
PASS on overlapping CIs, malformed / huge inputs and float-boundary sweeps.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import math
import random
import time
from fractions import Fraction
from typing import Any

import pytest
from mf_eval_world import NOW, ORIGIN, config_id, fabric, group, records

from meshforge.bench.stats import bootstrap_mean_ci
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    CalibrationConfig,
    MatchedRun,
    TermObservation,
)
from meshforge.controller.planner_eval import heldout as heldout_mod
from meshforge.controller.planner_eval.core import evaluate, group_regret, vote
from meshforge.controller.planner_eval.heldout import (
    CalibrationChoice,
    held_out,
    leaked_choices,
    refit_choices,
)
from meshforge.controller.planner_eval.mapping import Pick, config_for
from meshforge.controller.planner_eval.model import (
    CRITERION_POLICY,
    ORACLE,
    UNMEASURED,
    ConfigRef,
    EvalConfig,
    EvalGroup,
    PolicySpec,
    default_policies,
)
from meshforge.controller.planner_eval.regret_gate import regret_gate
from meshforge.controller.planner_eval.report import render_markdown, report_text

pytestmark = pytest.mark.unit

FAST_B = {"node_a": 100.0, "node_b": 10.0, "node_c": 50.0, "node_d": 80.0}
CFG = EvalConfig(bootstrap_resamples=300)
COST = CRITERION_POLICY.key  # calibrated cost_model, min_time
COST_U = "cost_model:uncalibrated:min_time"
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def _eval(groups, recs, calibrations=None, policies=None, **kw):  # type: ignore[no-untyped-def]
    return evaluate(
        groups,
        recs,
        calibrations or {},
        policies or default_policies(),
        kw.pop("config", CFG),
        campaign_ids=["camp_1"],
        **kw,
    )


def _pick(report: dict[str, Any], group_id: str, key: str) -> dict[str, Any]:
    (g,) = [g for g in report["groups"] if g["group_id"] == group_id]
    (p,) = [p for p in g["picks"] if p["key"] == key]
    return p


def _row(report: dict[str, Any], key: str) -> dict[str, Any]:
    (r,) = [r for r in report["policies"] if r["key"] == key]
    return r


def _verdict(report: dict[str, Any], criterion: int) -> dict[str, Any]:
    (v,) = [v for v in report["verdicts"] if v["criterion"] == criterion]
    return v


# ---------------------------------------------------------------- known answers


def test_known_answer_relative_and_absolute_regret() -> None:
    """cost_model picks node_b (best, 100 ms); Local picks node_a (350 ms)."""
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": 350, "node_b": 100, "node_c": 200, "node_d": 250})
    report = _eval([g], records(g, request, snapshot))
    cost = _pick(report, "g1", COST_U)
    assert cost["config_id"] == config_id("g1", "mode1", "node_b")
    assert (cost["regret_pct"], cost["regret_ms"]) == (0.0, 0.0)
    local = _pick(report, "g1", "least_loaded")
    # relative = (350-100)/100*100 = 250 %, absolute = 250 ms - both reported
    assert local["config_id"] == config_id("g1", "local", "node_a")
    assert (local["regret_pct"], local["regret_ms"]) == (250.0, 250.0)
    oracle = _pick(report, "g1", ORACLE)
    assert oracle["config_id"] == g.best_config_id and oracle["regret_pct"] == 0.0


def test_regret_is_relative_to_best_not_to_the_pick() -> None:
    """Guard against the (pick-best)/pick formula: 300 vs 100 is 200 %, not 66 %."""
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200, "node_d": 250})
    report = _eval([g], records(g, request, snapshot))
    assert _pick(report, "g1", "least_loaded")["regret_pct"] == 200.0
    assert _row(report, "least_loaded")["mean_regret_ms"] == 200.0


def test_tie_set_pick_has_regret_zero_even_if_slower_than_lowest_median() -> None:
    """A tie label means statistically indistinguishable: a pick inside it is 0."""
    request, snapshot = fabric(FAST_B)
    # b is the lowest median but a is in the tie set: Local (node_a) -> regret 0
    g = group(
        "g1",
        {"node_a": 105, "node_b": 100, "node_c": 400, "node_d": 400},
        label="tie",
        tied=("node_a", "node_b"),
    )
    report = _eval([g], records(g, request, snapshot))
    assert _pick(report, "g1", "least_loaded")["regret_pct"] == 0.0
    assert _pick(report, "g1", COST_U)["regret_pct"] == 0.0
    # outside the tie set: relative to the lowest median
    g2 = group(
        "g2",
        {"node_a": 300, "node_b": 100, "node_c": 100, "node_d": 100},
        label="tie",
        tied=("node_b", "node_c", "node_d"),
    )
    r2 = _eval([g2], records(g2, request, snapshot))
    assert _pick(r2, "g2", "least_loaded")["regret_pct"] == 200.0


@pytest.mark.parametrize("label", ["insufficient", "pending"])
def test_insufficient_and_pending_are_reported_never_counted(label: str) -> None:
    request, snapshot = fabric(FAST_B)
    g = group(
        "g1",
        {"node_a": 100, "node_b": 100},
        label=label,
        reason="why not",
    )
    report = _eval([g], records(g, request, snapshot))
    (out,) = report["groups"]
    assert out["status"] == "excluded" and label in out["excluded_reason"]
    assert out["picks"] == []
    assert report["counts"]["labels"][label] == 1
    assert report["counts"]["evaluated_groups"] == 0
    assert all(r["counted_groups"] == 0 for r in report["policies"])
    assert all(r["mean_regret_pct"] is None for r in report["policies"])


def test_no_replay_group_is_reported_not_counted_and_skips_are_tallied() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": 100, "node_b": 50})
    report = _eval([g], records(g, request, snapshot, replay=None))
    assert report["groups"][0]["status"] == "no_replay"
    assert report["counts"]["records_skipped"]["no_replay"] == 6
    assert report["counts"]["no_replay_groups"] == 1
    assert _row(report, COST)["counted_groups"] == 0
    # forced run without a shadow: the pinned request is not a preference
    report = _eval([g], records(g, request, snapshot, shadow=False))
    assert report["counts"]["records_skipped"]["no_shadow"] == 6
    assert report["groups"][0]["status"] == "no_replay"


@pytest.mark.parametrize(
    "junk", [{}, {"v": 1}, [], 7, "x" * 100_000, {"request": None}, {"snapshot": []}]
)
def test_unreadable_replays_are_skipped_not_crashing(junk: Any) -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": 100, "node_b": 50})
    report = _eval([g], records(g, request, snapshot, replay=junk))
    assert report["groups"][0]["status"] == "no_replay"
    assert sum(report["counts"]["records_skipped"].values()) == 6
    assert report["counts"]["records_used"] == 0


def test_partially_missing_replays_use_the_rest() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": 100, "node_b": 50})
    recs = records(g, request, snapshot)
    recs[0] = dataclasses.replace(recs[0], replay=None)
    recs[1] = dataclasses.replace(recs[1], replay={"garbage": 1})
    report = _eval([g], recs)
    assert report["groups"][0]["status"] == "evaluated"
    assert report["counts"]["records_used"] == 4 and report["counts"]["records"] == 6
    assert sum(report["counts"]["records_skipped"].values()) == 2


def test_unmeasured_pick_is_not_regret_zero_and_not_counted() -> None:
    """cost_model picks node_b but node_b has no measured configuration."""
    request, snapshot = fabric(FAST_B)
    g = group(
        "g1",
        {"node_a": 100, "node_b": 50, "node_c": 200},
        unsupported=("node_b",),
    )
    report = _eval([g], records(g, request, snapshot))
    p = _pick(report, "g1", COST_U)
    assert p["status"] == "unmeasured" and p["regret_pct"] is None
    assert _row(report, COST_U)["counted_groups"] == 0
    assert _row(report, COST_U)["unmeasured_groups"] == 1
    assert _row(report, COST_U)["mean_regret_pct"] is None


# ---------------------------------------------------------------- D313 votes


def test_vote_known_answers() -> None:
    assert vote({}) is None
    assert vote({"b": 2, "a": 1}) == "b"
    assert vote({"b": 2, "a": 2}) == "a"  # ties -> smallest config id
    assert vote({"cfg_z": 1, "cfg_a": 1, "cfg_m": 1}) == "cfg_a"
    assert vote({UNMEASURED: 2, "a": 2}) == UNMEASURED  # unmeasured wins its tie
    assert vote({UNMEASURED: 1, "a": 2}) == "a"  # but loses a majority
    assert vote({UNMEASURED: 2, "a": 1}) == UNMEASURED


def test_vote_is_independent_of_dict_order() -> None:
    items = [("c3", 2), ("c1", 2), ("c2", 2), ("c0", 1)]
    for perm in itertools.permutations(items):
        assert vote(dict(perm)) == "c1"


def test_group_pick_is_majority_of_all_records_not_the_first() -> None:
    """Two records see node_b fastest, one sees node_c fastest -> majority wins."""
    req_b, snap_b = fabric(FAST_B)
    req_c, snap_c = fabric(
        {"node_a": 100.0, "node_b": 50.0, "node_c": 10.0, "node_d": 80.0}
    )
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200})
    recs = records(g, req_b, snap_b, reps=1)
    odd = records(g, req_c, snap_c, reps=1)
    mixed = [recs[0], recs[1], odd[2]]  # configs a, b (see b) + c (sees c)
    mixed = [
        dataclasses.replace(r, repetition=i + 1, attempt_id=f"att{i}")
        for i, r in enumerate(mixed)
    ]
    report = _eval([g], mixed)
    pick = _pick(report, "g1", COST_U)
    assert pick["votes"] == {
        config_id("g1", "mode1", "node_b"): 2,
        config_id("g1", "mode1", "node_c"): 1,
    }
    assert pick["config_id"] == config_id("g1", "mode1", "node_b")
    # reverse: majority node_c
    mixed2 = [odd[0], odd[1], recs[2]]
    mixed2 = [
        dataclasses.replace(r, repetition=i + 1, attempt_id=f"att{i}")
        for i, r in enumerate(mixed2)
    ]
    pick2 = _pick(_eval([g], mixed2), "g1", COST_U)
    assert pick2["config_id"] == config_id("g1", "mode1", "node_c")


def test_even_split_vote_goes_to_smallest_config_id() -> None:
    req_b, snap_b = fabric(FAST_B)
    req_c, snap_c = fabric(
        {"node_a": 100.0, "node_b": 50.0, "node_c": 10.0, "node_d": 80.0}
    )
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200})
    rb = records(g, req_b, snap_b, reps=1)
    rc = records(g, req_c, snap_c, reps=1)
    recs = [
        dataclasses.replace(rb[0], attempt_id="x1"),
        dataclasses.replace(rc[1], attempt_id="x2"),
    ]
    pick = _pick(_eval([g], recs), "g1", COST_U)
    assert pick["config_id"] == min(pick["votes"])  # node_b's id < node_c's id
    assert pick["config_id"] == config_id("g1", "mode1", "node_b")


# ---------------------------------------------------------------- D314 mapping extras


def test_mode3_set_is_exact_not_subset_or_superset() -> None:
    g = group("g", {"node_a": 1.0, "node_b": 1.0}, sets={("node_b", "node_c"): 2.0})
    assert config_for(Pick("mode3", "node_b", ("node_b",)), ORIGIN, g.configs) is None
    assert (
        config_for(
            Pick("mode3", "node_b", ("node_b", "node_c", "node_d")), ORIGIN, g.configs
        )
        is None
    )
    assert config_for(Pick("mode3", "node_b", ("node_b", "node_c")), ORIGIN, g.configs)
    # the same set under another origin is a different configuration
    assert (
        config_for(Pick("mode3", "node_b", ("node_b", "node_c")), "node_z", g.configs)
        is None
    )


def test_mode3_and_mode1_configs_do_not_cross_match() -> None:
    cfgs = (
        ConfigRef(
            "c1",
            "mode3",
            "node_b",
            ORIGIN,
            node_set=("node_b", "node_c"),
            median_ms=5,
            repetitions=3,
        ),
    )
    assert config_for(Pick("mode1", "node_b"), ORIGIN, cfgs) is None
    assert config_for(Pick("local", "node_b"), ORIGIN, cfgs) is None


# ---------------------------------------------------------------- D316 held-out


def _run(
    n: int, family: str | None, fingerprint: str | None, node: str = "node_b"
) -> MatchedRun:
    return MatchedRun(
        decision_id=n,
        attempt_id=f"a{n}",
        node_id=node,
        finished_at=T0 + dt.timedelta(minutes=n),
        terms=(TermObservation("compute", 10.0, 100.0, fitted=True),),
        family=family,
        fingerprint=fingerprint,
    )


def test_held_out_filter_known_answer() -> None:
    runs = [
        _run(1, "F", "p1"),  # evaluated family -> out
        _run(2, "G", "p1"),  # other family but same fingerprint as F's record -> out
        _run(3, None, "p1"),  # user job of the same workload -> out
        _run(4, "G", "p2"),  # kept
        _run(5, None, None),  # kept (no family, no fingerprint)
        _run(6, "FF", "p3"),  # a family whose name merely starts with F -> kept
        _run(7, "f", "p4"),  # case differs -> a different family -> kept
        _run(8, "F", "p9"),  # evaluated family, unrelated fingerprint -> out
    ]
    kept, removed = held_out(runs, "F", frozenset({"p1"}))
    assert sorted(r.decision_id for r in removed) == [1, 2, 3, 8]
    assert sorted(r.decision_id for r in kept) == [4, 5, 6, 7]
    # input is not mutated; partition is complete and disjoint
    assert len(kept) + len(removed) == len(runs)


def test_refit_never_feeds_the_evaluated_family_to_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spy on what ``fit`` is handed for every family (leave-one-family-out)."""
    seen: dict[int, list[MatchedRun]] = {}
    real = heldout_mod.fit

    def spy(runs: Any, config: Any) -> Any:
        runs = list(runs)
        seen[len(seen)] = runs
        return real(runs, config)

    monkeypatch.setattr(heldout_mod, "fit", spy)
    runs = [_run(n, fam, f"fp_{fam}") for n, fam in enumerate(["A", "B", "C"] * 30, 1)]
    families = {
        "A": frozenset({"fp_A"}),
        "B": frozenset({"fp_B"}),
        "C": frozenset({"fp_C"}),
    }
    out = refit_choices(runs, families, CalibrationConfig())
    assert list(out) == ["A", "B", "C"]  # sorted, one fit per family
    for i, fam in enumerate(["A", "B", "C"]):
        assert fam not in {r.family for r in seen[i]}, fam
        assert f"fp_{fam}" not in {r.fingerprint for r in seen[i]}
        assert len(seen[i]) == 60
        assert out[fam].fitted_runs == 60 and out[fam].excluded_runs == 30
        assert out[fam].source == "refit"


def test_refit_with_only_the_evaluated_family_plans_uncalibrated() -> None:
    runs = [_run(n, "A", "fp") for n in range(1, 40)]
    out = refit_choices(runs, {"A": frozenset({"fp"})}, CalibrationConfig())
    assert out["A"].source == "none" and out["A"].calibration is None
    assert out["A"].fitted_runs == 0 and out["A"].excluded_runs == 39


def test_refit_with_no_runs_at_all() -> None:
    out = refit_choices([], {"A": frozenset()}, CalibrationConfig())
    assert out["A"].source == "none"


def test_allow_leak_stamps_every_verdict_and_markdown_banner() -> None:
    request, snapshot = fabric(FAST_B)
    groups = [
        group(f"g{i:02d}", {"node_a": 300, "node_b": 100, "node_c": 200})
        for i in range(12)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    clean = _eval(groups, recs)
    leaked = _eval(groups, recs, leaked=True)
    assert clean["leaked"] is False and leaked["leaked"] is True
    assert {v["status"] for v in leaked["verdicts"]} == {"leaked"}
    assert all(v["would_be"] is not None for v in leaked["verdicts"])
    assert [v["would_be"] for v in leaked["verdicts"]] == [
        v["status"] for v in clean["verdicts"]
    ]
    assert all(v["would_be"] is None for v in clean["verdicts"])
    assert "LEAKED" in render_markdown(leaked)
    assert "LEAKED" not in render_markdown(clean)
    # numbers other than the stamp are unchanged
    assert leaked["policies"] == clean["policies"]


def test_leaked_choices_are_active_for_every_family() -> None:
    out = leaked_choices(["B", "A", "A"], None)
    assert list(out) == ["A", "B"] and all(c.source == "none" for c in out.values())


# ---------------------------------------------------------------- D317 stats / verdicts


def _world(n_good: int, n_bad: int) -> tuple[list[EvalGroup], list[Any]]:
    """cost_model picks node_b. In good groups node_b is best; in bad groups
    node_b is 4x slower than node_c. Rule policies pick node_a (Local)."""
    request, snapshot = fabric(FAST_B)
    groups = [
        group(f"g{i:03d}", {"node_a": 900, "node_b": 100, "node_c": 300})
        for i in range(n_good)
    ] + [
        group(f"h{i:03d}", {"node_a": 900, "node_b": 400, "node_c": 100})
        for i in range(n_bad)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    return groups, recs


@pytest.mark.parametrize(
    ("good", "bad", "status"),
    [
        (9, 0, "too_few_groups"),
        (10, 0, "PASS"),
        (8, 2, "PASS"),
        (7, 3, "FAIL"),
        (16, 4, "PASS"),
        (15, 5, "FAIL"),
    ],
)
def test_criterion_1_boundaries(good: int, bad: int, status: str) -> None:
    groups, recs = _world(good, bad)
    report = _eval(groups, recs)
    v = _verdict(report, 1)
    assert v["status"] == status, v
    assert v["counted_groups"] == good + bad


def test_criterion_1_threshold_is_inclusive_at_15_percent() -> None:
    """pick exactly 15 % slower counts as within; 15.01 % does not."""
    request, snapshot = fabric(FAST_B)
    groups = [
        group(f"g{i:02d}", {"node_a": 900, "node_b": 115, "node_c": 100})
        for i in range(10)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    assert _verdict(_eval(groups, recs), 1)["status"] == "PASS"
    groups = [
        group(f"g{i:02d}", {"node_a": 900, "node_b": 115.2, "node_c": 100})
        for i in range(10)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    assert _verdict(_eval(groups, recs), 1)["status"] == "FAIL"


def test_never_pass_with_too_few_groups_even_when_perfect() -> None:
    for n in range(10):
        groups, recs = _world(n, 0)
        report = _eval(groups, recs)
        for c in (1, 2):
            assert _verdict(report, c)["status"] == "too_few_groups", (n, c)


def test_empty_campaign_does_not_crash() -> None:
    report = _eval([], [])
    assert report["counts"]["groups"] == 0
    assert {v["status"] for v in report["verdicts"]} == {"too_few_groups"}
    assert all(r["mean_regret_pct"] is None for r in report["policies"])
    report_text(report)
    render_markdown(report)


def test_criterion_2_pass_requires_cost_ci_strictly_below_every_rule_ci() -> None:
    groups, recs = _world(24, 0)
    report = _eval(groups, recs)
    v = _verdict(report, 2)
    cost = _row(report, COST)
    rules = [r for r in report["policies"] if r["kind"] == "rule"]
    assert rules
    if v["status"] == "PASS":
        assert all(cost["ci_pct"]["high"] < r["ci_pct"]["low"] for r in rules)
    else:  # pragma: no cover - documents the converse
        assert any(not cost["ci_pct"]["high"] < r["ci_pct"]["low"] for r in rules)


def test_criterion_2_never_pass_when_cis_overlap_or_touch() -> None:
    """All groups tied between node_a and node_b: every policy that picks one
    of them has regret 0 -> identical, touching CIs [0, 0]. Never PASS."""
    request, snapshot = fabric(FAST_B)
    groups = [
        group(
            f"g{i:02d}",
            {"node_a": 100, "node_b": 100, "node_c": 100, "node_d": 100},
            label="tie",
            tied=("node_a", "node_b", "node_c", "node_d"),
        )
        for i in range(14)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    report = _eval(groups, recs)
    assert _row(report, COST)["ci_pct"] == {"low": 0.0, "high": 0.0}
    assert _verdict(report, 2)["status"] == "not_separable"
    assert _verdict(report, 1)["status"] == "PASS"  # criterion 1 is independent


def test_criterion_2_fail_when_a_rule_policy_is_clearly_better() -> None:
    """node_a (Local) is best everywhere; cost_model is fooled into node_b."""
    request, snapshot = fabric(FAST_B)
    groups = [
        group(f"g{i:02d}", {"node_a": 100, "node_b": 500, "node_c": 700, "node_d": 800})
        for i in range(14)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    report = _eval(groups, recs)
    assert _verdict(report, 2)["status"] == "FAIL"
    assert _verdict(report, 1)["status"] == "FAIL"


def test_criterion_2_too_few_when_any_compared_rule_policy_is_short() -> None:
    """A rule policy with <10 counted groups blocks a PASS (unmeasured picks)."""
    request, snapshot = fabric(FAST_B)
    # node_a (where rule policies land) has no measured config for 5 of 15 groups
    groups = [
        group(
            f"g{i:02d}",
            {"node_a": 900, "node_b": 100, "node_c": 300},
            unsupported=("node_a",) if i < 6 else (),
        )
        for i in range(15)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    report = _eval(groups, recs)
    assert _verdict(report, 2)["status"] in {"too_few_groups", "not_separable", "FAIL"}
    assert _verdict(report, 2)["status"] != "PASS" or all(
        r["counted_groups"] >= 10 for r in report["policies"] if r["kind"] == "rule"
    )


def test_bootstrap_ci_brackets_the_mean_and_is_seed_stable() -> None:
    values = [0.0, 10.0, 200.0, 0.0, 3.5, 99.0, 0.0, 1.0, 1.0, 40.0]
    a = bootstrap_mean_ci(values, seed=5, resamples=2000)
    b = bootstrap_mean_ci(list(reversed(values)), seed=5, resamples=2000)
    assert a == b
    assert a.low <= a.mean <= a.high
    assert bootstrap_mean_ci(values, seed=6, resamples=2000) != a
    one = bootstrap_mean_ci([7.0], seed=1)
    assert (one.low, one.mean, one.high) == (7.0, 7.0, 7.0)
    for bad in ([],):
        with pytest.raises(ValueError):
            bootstrap_mean_ci(bad, seed=1)
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1.0, 2.0], seed=-1)
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1.0, 2.0], seed=1, level=1.0)
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1.0, 2.0], seed=1, resamples=0)


def test_bootstrap_nan_inf_is_not_silently_accepted() -> None:
    for bad in (float("nan"), float("inf")):
        try:
            ci = bootstrap_mean_ci([1.0, bad, 2.0], seed=1, resamples=50)
        except (ValueError, TypeError):
            continue
        # if accepted it must not hand back a finite-looking CI
        assert not all(math.isfinite(x) for x in (ci.low, ci.high, ci.mean))


def test_report_is_byte_identical_and_input_order_independent() -> None:
    groups, recs = _world(8, 6)
    base = report_text(_eval(groups, recs))
    assert report_text(_eval(groups, recs)) == base  # repeat
    rng = random.Random(3)  # noqa: S311
    for _ in range(4):
        g2, r2 = list(groups), list(recs)
        rng.shuffle(g2)
        rng.shuffle(r2)
        assert report_text(_eval(g2, r2)) == base
    # different campaign id ordering is normalised too
    a = evaluate(
        groups, recs, {}, default_policies(), CFG, campaign_ids=["b", "a", "a"]
    )
    assert a["campaign_ids"] == ["a", "b"]
    # no NaN / Infinity token ever reaches the text
    assert "NaN" not in base and "Infinity" not in base
    # markdown is deterministic as well
    assert render_markdown(_eval(groups, recs)) == render_markdown(_eval(groups, recs))


def test_evaluate_does_not_mutate_its_inputs() -> None:
    groups, recs = _world(3, 1)
    before = (repr(groups), repr(recs))
    _eval(groups, recs)
    assert (repr(groups), repr(recs)) == before


def test_holdout_restricts_to_one_family() -> None:
    request, snapshot = fabric(FAST_B)
    a = group("ga", {"node_a": 300, "node_b": 100}, family="fam.a")
    b = group("gb", {"node_a": 300, "node_b": 100}, family="fam.b")
    recs = records(a, request, snapshot) + records(b, request, snapshot)
    report = _eval([a, b], recs, holdout="fam.b")
    assert [g["group_id"] for g in report["groups"]] == ["gb"]
    assert report["holdout"] == "fam.b"
    none = _eval([a, b], recs, holdout="fam.none")
    assert none["groups"] == []


# ---------------------------------------------------------------- malformed input


def test_duplicate_policy_rows_are_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        evaluate(
            [],
            [],
            {},
            (PolicySpec("random"), PolicySpec("random")),
            CFG,
            campaign_ids=[],
        )


@pytest.mark.parametrize(
    "median",
    [
        0.0,
        -5.0,
        pytest.param(
            float("nan"),
            marks=pytest.mark.xfail(
                strict=True,
                raises=ValueError,
                reason="DEFECT T3 (minor): a non-finite median reaches report_text "
                "(allow_nan=False) -> unhandled ValueError (HTTP 500)",
            ),
        ),
        pytest.param(
            float("inf"),
            marks=pytest.mark.xfail(
                strict=True,
                raises=ValueError,
                reason="DEFECT T3 (minor): see nan",
            ),
        ),
    ],
)
def test_non_positive_or_non_finite_medians_never_crash_or_poison(
    median: float,
) -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": median, "node_b": median})
    recs = records(g, request, snapshot)
    report = _eval([g], recs)
    text = report_text(report)  # allow_nan=False -> must not raise
    assert "NaN" not in text
    assert report["groups"][0]["status"] in {"excluded", "evaluated", "no_replay"}
    render_markdown(report)


def test_records_of_unknown_groups_or_configs_are_ignored_or_skipped() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("g1", {"node_a": 100, "node_b": 50})
    ghost = group("ghost", {"node_a": 100, "node_b": 50})
    stray = records(ghost, request, snapshot)
    weird = [
        dataclasses.replace(r, config_id="cfg_unknown")
        for r in records(g, request, snapshot)
    ]
    report = _eval([g], stray + weird)
    assert report["groups"][0]["status"] == "no_replay"
    assert report["counts"]["records_skipped"]["replay_unplannable"] == 6


def test_group_regret_unit_cases() -> None:
    g = group(
        "g", {"node_a": 100, "node_b": 150, "node_c": 400}, unsupported=("node_c",)
    )
    assert group_regret(g, g.best_config_id or "", 100.0) == (0.0, 0.0)
    assert group_regret(g, config_id("g", "mode1", "node_b"), 100.0) == (50.0, 50.0)
    assert group_regret(g, config_id("g", "mode1", "node_c"), 100.0) is None
    assert group_regret(g, "nope", 100.0) is None


def test_unicode_family_and_ids_survive_report_and_markdown() -> None:
    request, snapshot = fabric(FAST_B)
    g = group("gé|1\n", {"node_a": 300, "node_b": 100}, family="fam|ünicøde\n# x")
    report = _eval([g], records(g, request, snapshot))
    md = render_markdown(report)
    for line in md.splitlines():
        # a pipe or newline in a family/id must not break a table row apart
        if line.startswith("| ") and "unic" in line:
            assert "\\|" in line
    assert report_text(report).encode("utf-8")


def test_large_campaign_is_deterministic_and_fast_enough() -> None:
    request, snapshot = fabric(FAST_B)
    groups = [
        group(f"g{i:04d}", {"node_a": 300, "node_b": 100, "node_c": 200, "node_d": 250})
        for i in range(150)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=2)]
    t0 = time.monotonic()
    report = _eval(groups, recs, config=EvalConfig())
    took = time.monotonic() - t0
    assert report["counts"]["records"] == 150 * 4 * 2
    assert took < 120, took  # generous; the point is it terminates
    assert _verdict(report, 1)["status"] == "PASS"


# ---------------------------------------------------------------- D319 gate


@pytest.mark.parametrize(
    ("active", "new", "status"),
    [
        (20.0, 21.0, "passed"),  # exactly x1.05
        (20.0, 21.0000001, "rejected"),
        (100.0, 105.0, "passed"),
        (100.0, 105.01, "rejected"),
        (0.0, 0.0, "passed"),
        (0.0, 0.001, "rejected"),
        (10.0, 5.0, "passed"),  # better
    ],
)
def test_gate_boundary(active: float, new: float, status: str) -> None:
    assert regret_gate(new, active, 10).status == status


def test_gate_group_count_boundary() -> None:
    assert regret_gate(99.0, 1.0, 9).status == "skipped"
    assert regret_gate(99.0, 1.0, 10).status == "rejected"
    assert regret_gate(99.0, 1.0, 0).status == "skipped"
    skipped = regret_gate(99.0, 1.0, 9)
    assert skipped.reason and "too_few_groups" in skipped.reason


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T1 (minor): D319 boundary x1.05 rejected for some decimals (float)",
)
def test_gate_exact_decimal_boundary_sweep_documented_defect() -> None:
    """Every (active, active x 1.05) pair that is exact in decimal must pass.

    DEFECT T1 (minor): ``active_mean * (1.0 + 0.05)`` in binary floating point
    rounds below the exact product for some values (active 1.14 -> limit
    1.1969999999999998), so a version exactly at x1.05 is rejected."""
    bad = []
    for thousandths in range(1, 4000):
        if (thousandths * 105) % 100:
            continue
        active = thousandths / 1000
        new = float(Fraction(thousandths * 105, 100_000))
        if regret_gate(new, active, 10).status != "passed":
            bad.append((active, new))
    assert not bad, bad[:5]


def test_regret_threshold_sweep_exact_15_percent_is_within() -> None:
    """Exact-15 % picks (representable medians) must count as within 15 %."""
    bad = []
    for k in range(1, 4000):
        best = k * 0.25
        pick = Fraction(k, 4) * Fraction(115, 100)
        if pick.denominator not in (1, 2, 4):
            continue
        cfgs = (
            ConfigRef("b", "local", "n", "n", median_ms=best, repetitions=3),
            ConfigRef("p", "mode1", "m", "n", median_ms=float(pick), repetitions=3),
        )
        g = EvalGroup("c", "g", "f", "none", "best", "b", (), cfgs)
        r = group_regret(g, "p", best)
        assert r is not None
        if r[0] > 15.0:
            bad.append((best, float(pick), r[0]))
    assert not bad, bad[:5]


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T2 (minor): relative regret is compared to the 15 % threshold "
    "unrounded; non-representable medians give 15.000000000000002 > 15",
)
def test_regret_threshold_float_noise_defect() -> None:
    best = 14.714285714285714
    pick = best * 1.15
    cfgs = (
        ConfigRef("b", "local", "n", "n", median_ms=best, repetitions=3),
        ConfigRef("p", "mode1", "m", "n", median_ms=pick, repetitions=3),
    )
    g = EvalGroup("c", "g", "f", "none", "best", "b", (), cfgs)
    r = group_regret(g, "p", best)
    assert r is not None and r[0] <= 15.0


# ---------------------------------------------------------------- oracle refused (D298)


def test_oracle_refused_everywhere_it_could_place_a_job() -> None:
    from meshforge.common.manifest import POLICIES, parse_manifest_data

    assert ORACLE not in POLICIES
    request, snapshot = fabric(FAST_B)
    with pytest.raises(ValueError):
        plan(request, snapshot, ORACLE, NOW)
    for variant in ("oracle", "ORACLE", " oracle"):
        with pytest.raises(Exception):  # noqa: B017 - any refusal
            parse_manifest_data(
                {"name": "x", "entrypoint": ["python", "m.py"], "policy": variant}
            )
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from meshforge.controller.api.job_models import PlacementRequest

    for variant in ("oracle", "ORACLE"):
        with pytest.raises(ValidationError):
            PlacementRequest.model_validate({"policy": variant})


def test_oracle_row_has_regret_zero_by_construction_everywhere() -> None:
    groups, recs = _world(6, 6)
    report = _eval(groups, recs)
    o = _row(report, ORACLE)
    assert o["mean_regret_pct"] == 0.0 and o["kind"] == "oracle"
    assert o["counted_groups"] == 12 and o["ci_pct"] == {"low": 0.0, "high": 0.0}
    # the oracle is never the criterion row nor a "rule policy" of criterion 2
    assert _verdict(report, 1)["counted_groups"] == _row(report, COST)["counted_groups"]


def test_calibrated_row_missing_choice_equals_uncalibrated() -> None:
    """No calibration for a family -> calibrated row plans uncalibrated (the
    service always supplies one per family; this pins the fallback)."""
    groups, recs = _world(3, 0)
    report = _eval(groups, recs)
    a = _pick(report, "g000", COST)
    b = _pick(report, "g000", COST_U)
    assert a["config_id"] == b["config_id"]
    _ = CalibrationChoice  # imported for type reference


def test_criterion_2_needs_ten_counted_groups_for_the_cost_model_row_itself() -> None:
    """Rule policies have >= 10 counted groups, calibrated cost_model only 8
    (its pick, node_b, was never measured in 22 of 30 groups): no verdict."""
    request, snapshot = fabric(FAST_B)
    groups = [
        group(
            f"g{i:02d}",
            {"node_a": 900, "node_b": 100, "node_c": 300, "node_d": 400},
            unsupported=("node_b",) if i < 22 else (),
        )
        for i in range(30)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    report = _eval(groups, recs)
    assert _row(report, COST)["counted_groups"] == 8
    assert _row(report, "least_loaded")["counted_groups"] == 30
    assert all(
        r["counted_groups"] >= 10 for r in report["policies"] if r["kind"] == "rule"
    )
    assert _verdict(report, 1)["status"] == "too_few_groups"
    assert _verdict(report, 2)["status"] == "too_few_groups"
