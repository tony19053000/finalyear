"""V0.14 G4 (D299, D316, D317): held-out calibrations, the leak stamp and the verdicts.

The fabric predicts ``node_b`` fastest; really ``node_c`` is (medians). Matched
runs that measured ``node_b``'s compute 10x slower than predicted teach a
calibration (factor clamped to 5) that makes calibrated ``cost_model`` pick
``node_c`` - regret 0 instead of 400 %.

* runs of **another** family are used by the refit -> calibrated row regret 0;
* runs of the **evaluated** family - or of its workload (same fingerprint, no
  family) - are removed by the held-out filter -> the calibrated row cannot use
  them (regret 400 %). With the filter removed this test fails (held-out
  mutation check);
* ``allow_leak`` uses the active version, stamps the report and every verdict
  ``leaked`` (``would_be`` keeps the numbers' verdict);
* verdicts: ``too_few_groups`` below 10 counted groups (never PASS), criterion
  1 PASS / FAIL at the 80 % boundary, criterion 2 PASS / FAIL /
  ``not_separable`` from the CIs.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from mf_eval_world import ORIGIN, config_id, fabric, group, records

from meshforge.bench.stats import bootstrap_mean_ci
from meshforge.controller.planner.calibration import (
    CalibrationConfig,
    MatchedRun,
    TermObservation,
    fit,
)
from meshforge.controller.planner_eval import core
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.heldout import (
    calibration_digest,
    held_out,
    leaked_choices,
    refit_choices,
)
from meshforge.controller.planner_eval.model import (
    CRITERION_POLICY,
    EvalConfig,
    PolicySpec,
    default_policies,
)
from meshforge.controller.planner_eval.report import render_markdown

pytestmark = pytest.mark.unit

PREDICTED = {"node_a": 100.0, "node_b": 10.0, "node_c": 30.0, "node_d": 80.0}
MEASURED = {"node_a": 900.0, "node_b": 500.0, "node_c": 100.0, "node_d": 700.0}
FAMILY = "cpu.matmul"
PRINT = "f" * 64
CAL = CalibrationConfig()
CFG = EvalConfig(bootstrap_resamples=300)
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def _runs(family: str | None, fingerprint: str | None = None) -> list[MatchedRun]:
    out: list[MatchedRun] = []
    n = 0
    for node, ratio in (
        ("node_b", 10.0),
        ("node_c", 1.0),
        ("node_a", 1.0),
        ("node_d", 1.0),
    ):
        for _ in range(20):
            n += 1
            out.append(
                MatchedRun(
                    decision_id=n,
                    attempt_id=f"att_{n}",
                    node_id=node,
                    finished_at=T0 + dt.timedelta(minutes=n),
                    terms=(
                        TermObservation("compute", 10.0, 10.0 * ratio, fitted=True),
                    ),
                    family=family,
                    fingerprint=fingerprint,
                )
            )
    return out


def _world() -> tuple[Any, Any]:
    request, snapshot = fabric(PREDICTED, fingerprint=PRINT)
    g = group("g1", MEASURED, family=FAMILY)
    return g, records(g, request, snapshot)


def _calibrated_pick(report: dict[str, Any]) -> dict[str, Any]:
    (g,) = report["groups"]
    (pick,) = [p for p in g["picks"] if p["key"] == CRITERION_POLICY.key]
    return pick


def _evaluate(runs: list[MatchedRun]) -> dict[str, Any]:
    g, recs = _world()
    choices = refit_choices(runs, {FAMILY: frozenset({PRINT})}, CAL)
    return evaluate(
        [g], recs, choices, default_policies(), CFG, campaign_ids=["camp_1"]
    )


def test_runs_of_another_family_calibrate_the_pick() -> None:
    report = _evaluate(_runs("cpu.sort", "a" * 64))
    pick = _calibrated_pick(report)
    assert pick["config_id"] == config_id("g1", "mode1", "node_c")
    assert pick["regret_pct"] == 0.0
    (g,) = report["groups"]
    (uncal,) = [p for p in g["picks"] if p["key"] == "cost_model:uncalibrated:min_time"]
    assert uncal["config_id"] == config_id("g1", "mode1", "node_b")
    assert uncal["regret_pct"] == 400.0
    (used,) = report["calibrations"]
    assert used["source"] == "refit" and used["fitted_runs"] == 80
    assert used["excluded_runs"] == 0 and used["factors"] > 0
    assert used["digest"] is not None and len(used["digest"]) == 64


def test_runs_of_the_evaluated_family_never_reach_its_calibration() -> None:
    """Held-out mutation check: drop the family clause of ``held_out`` and the
    calibrated row would pick node_c (regret 0) - this asserts it cannot."""
    report = _evaluate(_runs(FAMILY, "a" * 64))
    pick = _calibrated_pick(report)
    assert pick["config_id"] == config_id("g1", "mode1", "node_b")
    assert pick["regret_pct"] == 400.0
    (used,) = report["calibrations"]
    assert used["source"] == "none" and used["fitted_runs"] == 0
    assert used["excluded_runs"] == 80 and used["digest"] is None


def test_runs_of_the_evaluated_workload_without_a_family_are_held_out_too() -> None:
    """Same fingerprint, no family (a user's job of the same workload)."""
    report = _evaluate(_runs(None, PRINT))
    assert _calibrated_pick(report)["regret_pct"] == 400.0
    assert report["calibrations"][0]["excluded_runs"] == 80


def test_held_out_filter_clauses() -> None:
    runs = _runs("cpu.sort", "a" * 64) + _runs(FAMILY) + _runs(None, PRINT)
    kept, removed = held_out(runs, FAMILY, frozenset({PRINT}))
    assert len(kept) == 80 and len(removed) == 160
    assert all(r.family != FAMILY and r.fingerprint != PRINT for r in kept)
    assert all(r.family == FAMILY or r.fingerprint == PRINT for r in removed)


def test_allow_leak_stamps_every_verdict_and_the_markdown() -> None:
    g, recs = _world()
    active = fit(_runs(FAMILY), CAL)  # fitted on the evaluated family itself
    report = evaluate(
        [g],
        recs,
        leaked_choices([FAMILY], active),
        default_policies(),
        CFG,
        campaign_ids=["camp_1"],
        leaked=True,
    )
    assert report["leaked"] is True
    assert _calibrated_pick(report)["regret_pct"] == 0.0  # it saw the answers
    for verdict in report["verdicts"]:
        assert verdict["status"] == "leaked"
        # V0.14 G6 (D324): criterion 3 has no decision here
        assert verdict["would_be"] == (
            "too_few_decisions" if verdict["criterion"] == 3 else "too_few_groups"
        )
    assert report["calibrations"][0]["source"] == "active"
    text = render_markdown(report)
    assert "**LEAKED**" in text
    assert "**LEAKED**" not in render_markdown({**report, "leaked": False})


def test_too_few_groups_is_never_pass() -> None:
    g, recs = _world()
    choices = refit_choices(_runs("cpu.sort"), {FAMILY: frozenset({PRINT})}, CAL)
    report = evaluate([g], recs, choices, default_policies(), CFG, campaign_ids=["c"])
    statuses = [v["status"] for v in report["verdicts"]]
    assert statuses == ["too_few_groups", "too_few_groups", "too_few_decisions"]


RULES = (
    "random",
    "round_robin",
    "least_loaded",
    "strongest_suitable",
    "resource_score",
    "cache_affinity",
)


def _row(key: str, n: int, within: float | None, unmeasured: int = 0) -> dict[str, Any]:
    return {
        "key": key,
        "counted_groups": n,
        "within_threshold_pct": within,
        "unmeasured_groups": unmeasured,
    }


def _rows(crit: dict[str, Any]) -> dict[str, Any]:
    rows = {CRITERION_POLICY.key: crit}
    for policy in RULES:
        rows[policy] = _row(policy, 12, 10.0)
    return rows


def _regrets(
    crit: list[float], rule: list[float], *, policies: tuple[str, ...] = RULES
) -> dict[str, dict[tuple[str, str], float]]:
    """Per-group regrets: the criterion row ``crit``, every rule policy ``rule``."""
    out = {CRITERION_POLICY.key: {("c", f"g{i}"): v for i, v in enumerate(crit)}}
    for policy in policies:
        out[policy] = {("c", f"g{i}"): v for i, v in enumerate(rule)}
    return out


def test_criterion_1_boundary_80_percent_passes() -> None:
    config = EvalConfig()
    regrets = _regrets([0.0] * 10, [5.0] * 10)
    passed = core._verdicts(_rows(_row("x", 10, 80.0)), regrets, config, False)
    assert passed[0]["status"] == "PASS" and passed[0]["comparisons"] == []
    failed = core._verdicts(_rows(_row("x", 10, 79.999)), regrets, config, False)
    assert failed[0]["status"] == "FAIL"
    few = core._verdicts(_rows(_row("x", 9, 100.0)), regrets, config, False)
    assert few[0]["status"] == "too_few_groups"


def test_criterion_2_paired_pass_fail_and_not_separable() -> None:
    config = EvalConfig(bootstrap_resamples=500)
    crit = _row("x", 12, 90.0)
    better = _regrets([0.0] * 12, [10.0, 20.0, 5.0] * 4)
    v = core._verdicts(_rows(crit), better, config, False)[1]
    assert v["status"] == "PASS", v
    assert len(v["comparisons"]) == 6
    assert all(c["ci_low"] > 0 and c["common_groups"] == 12 for c in v["comparisons"])
    assert v["value"] == min(c["ci_low"] for c in v["comparisons"])
    assert v["threshold"] == 0.0
    # identical regrets: every difference is 0 -> the CI touches 0, never PASS
    same = _regrets([3.0] * 12, [3.0] * 12)
    assert core._verdicts(_rows(crit), same, config, False)[1]["status"] == (
        "not_separable"
    )
    worse = _regrets([30.0] * 12, [10.0, 0.0] * 6)
    assert core._verdicts(_rows(crit), worse, config, False)[1]["status"] == "FAIL"
    leaked = core._verdicts(_rows(crit), better, config, True)
    assert [v["status"] for v in leaked] == ["leaked", "leaked"]
    assert [v["would_be"] for v in leaked] == ["PASS", "PASS"]


def test_criterion_2_needs_ten_common_groups_with_every_rule_policy() -> None:
    config = EvalConfig(bootstrap_resamples=200)
    regrets = _regrets([0.0] * 12, [10.0] * 12)
    # random was counted on other groups only: 3 in common
    regrets["random"] = {("c", f"g{i}"): 10.0 for i in range(9, 20)}
    rows = _rows(_row("x", 12, 90.0))
    rows["random"] = _row("random", 11, 10.0, unmeasured=9)
    v = core._verdicts(rows, regrets, config, False)[1]
    assert v["status"] == "too_few_groups" and v["counted_groups"] == 3
    (random_row,) = [c for c in v["comparisons"] if c["key"] == "random"]
    assert random_row["common_groups"] == 3 and random_row["ci_low"] is None
    assert random_row["unmeasured_groups"] == 9


def test_selection_bias_is_not_rewarded_by_the_pairing() -> None:
    """A rule policy whose pick is unmeasured on the 10 hard groups keeps only
    the 10 easy ones, where it ties cost_model (regret 0 both). cost_model is
    counted on all 20 and pays some regret on the hard ones.

    Unpaired (the pre-D322 rule): the rule policy's mean regret 0 is lower
    than cost_model's - cost_model could never PASS, and a policy that only
    ever answers easy questions would look best. Paired: on the 10 common
    groups the difference is exactly 0 -> ``not_separable``, the honest
    answer (and the verdict shows the rule policy's 10 unmeasured groups)."""
    config = EvalConfig(bootstrap_resamples=500)
    crit = {("c", f"g{i}"): (0.0 if i < 10 else 40.0) for i in range(20)}
    easy_only = {("c", f"g{i}"): 0.0 for i in range(10)}
    regrets = {CRITERION_POLICY.key: crit}
    rows = {CRITERION_POLICY.key: _row("x", 20, 50.0)}
    for policy in RULES:
        regrets[policy] = {("c", f"g{i}"): 25.0 for i in range(20)}
        rows[policy] = _row(policy, 20, 10.0)
    regrets["cache_affinity"] = easy_only
    rows["cache_affinity"] = _row("cache_affinity", 10, 100.0, unmeasured=10)
    v = core._verdicts(rows, regrets, config, False)[1]
    assert v["status"] == "not_separable", v
    (biased,) = [c for c in v["comparisons"] if c["key"] == "cache_affinity"]
    assert biased["common_groups"] == 10 and biased["mean_diff_pct"] == 0.0
    assert biased["unmeasured_groups"] == 10
    assert "unmeasured" in v["detail"]
    # ... and the reverse bias: a criterion policy that is unmeasured on the
    # hard groups cannot PASS on its easy ones against a policy that answered
    # them all - the pairing compares only what both answered
    rows2 = {CRITERION_POLICY.key: _row("x", 10, 100.0, unmeasured=10)}
    regrets2 = {CRITERION_POLICY.key: easy_only}
    for policy in RULES:
        regrets2[policy] = {("c", f"g{i}"): (0.0 if i < 10 else 5.0) for i in range(20)}
        rows2[policy] = _row(policy, 20, 90.0)
    v2 = core._verdicts(rows2, regrets2, config, False)[1]
    assert v2["status"] == "not_separable"
    # the unpaired rule would have PASSed: cost_model's CI is [0, 0] and every
    # rule policy's own CI lies above it
    unpaired = bootstrap_mean_ci(
        list(regrets2["random"].values()), seed=1, resamples=500
    )
    assert unpaired.low > 0.0


def test_selection_bias_end_to_end_unpaired_would_have_passed() -> None:
    """Calibrated cost_model's pick (node_b) is never measured on the hard
    groups; on the 10 easy groups it is the best. Rule policies (node_a) are
    measured everywhere and lose 50 % on the easy groups, 0 % on the hard ones
    (node_a is best there). Unpaired: cost_model's mean 0 vs rules' mean > 0
    with separate CIs would PASS; paired on the 10 common groups it still
    PASSes only because it really is better there - so the hard groups must
    not change the verdict, and they do not."""
    request, snapshot = fabric(PREDICTED, fingerprint=PRINT)
    easy = [
        group(f"e{i:02d}", {"node_a": 150, "node_b": 100, "node_c": 400}, family=FAMILY)
        for i in range(10)
    ]
    hard = [
        group(f"h{i:02d}", {"node_a": 100, "node_c": 400}, family=FAMILY)
        for i in range(10)
    ]
    groups = easy + hard
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    report = evaluate(
        groups, recs, {}, default_policies(), CFG, campaign_ids=["camp_1"]
    )
    c2 = report["verdicts"][1]
    (cost,) = [p for p in report["policies"] if p["key"] == CRITERION_POLICY.key]
    assert cost["unmeasured_groups"] == 10 and cost["counted_groups"] == 10
    for comparison in c2["comparisons"]:
        if comparison["key"] == "random":
            continue  # random's picks vary
        assert comparison["common_groups"] == 10
    assert f"unmeasured in {cost['unmeasured_groups']} group(s)" in c2["detail"]


def test_twelve_groups_end_to_end_reach_real_verdicts() -> None:
    request, snapshot = fabric(PREDICTED, fingerprint=PRINT)
    groups = [
        group(f"g{i:02d}", {k: v + i for k, v in MEASURED.items()}, family=FAMILY)
        for i in range(12)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    choices = refit_choices(_runs("cpu.sort"), {FAMILY: frozenset({PRINT})}, CAL)
    report = evaluate(
        groups, recs, choices, default_policies(), CFG, campaign_ids=["camp_1"]
    )
    c1, c2, c3 = report["verdicts"]  # V0.14 G6: + criterion 3 (no decisions here)
    assert c3["criterion"] == 3 and c3["status"] == "too_few_decisions"
    assert c1["status"] == "PASS" and c1["value"] == 100.0
    assert c1["counted_groups"] == 12
    # every rule policy here picks a slower node in every group
    assert c2["status"] == "PASS", c2


def test_calibration_digest_is_stable_and_none_for_nothing() -> None:
    fitted = fit(_runs("cpu.sort"), CAL)
    assert calibration_digest(fitted) == calibration_digest(fit(_runs("cpu.sort"), CAL))
    assert calibration_digest(None) is None
    assert calibration_digest(fit([], CAL)) is None


def test_rule_policies_ignore_the_calibration() -> None:
    g, recs = _world()
    with_cal = evaluate(
        [g],
        recs,
        refit_choices(_runs("cpu.sort"), {FAMILY: frozenset({PRINT})}, CAL),
        [PolicySpec("least_loaded")],
        CFG,
        campaign_ids=["c"],
    )
    without = evaluate(
        [g], recs, {}, [PolicySpec("least_loaded")], CFG, campaign_ids=["c"]
    )
    assert with_cal["groups"][0]["picks"] == without["groups"][0]["picks"]
    assert with_cal["groups"][0]["picks"][0]["config_id"] == config_id(
        "g1", "local", ORIGIN
    )
