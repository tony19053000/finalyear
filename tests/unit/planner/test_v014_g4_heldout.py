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
        assert verdict["would_be"] == "too_few_groups"
    assert report["calibrations"][0]["source"] == "active"
    text = render_markdown(report)
    assert "**LEAKED**" in text
    assert "**LEAKED**" not in render_markdown({**report, "leaked": False})


def test_too_few_groups_is_never_pass() -> None:
    g, recs = _world()
    choices = refit_choices(_runs("cpu.sort"), {FAMILY: frozenset({PRINT})}, CAL)
    report = evaluate([g], recs, choices, default_policies(), CFG, campaign_ids=["c"])
    statuses = [v["status"] for v in report["verdicts"]]
    assert statuses == ["too_few_groups", "too_few_groups"]


def _row(
    key: str, n: int, within: float | None, low: float, high: float
) -> dict[str, Any]:
    return {
        "key": key,
        "counted_groups": n,
        "within_threshold_pct": within,
        "ci_pct": {"low": low, "high": high},
    }


def _rows(crit: dict[str, Any], rule_ci: tuple[float, float]) -> dict[str, Any]:
    rows = {CRITERION_POLICY.key: crit}
    for policy in (
        "random",
        "round_robin",
        "least_loaded",
        "strongest_suitable",
        "resource_score",
        "cache_affinity",
    ):
        rows[policy] = _row(policy, 12, 10.0, *rule_ci)
    return rows


def test_criterion_1_boundary_80_percent_passes() -> None:
    config = EvalConfig()
    passed = core._verdicts(_rows(_row("x", 10, 80.0, 0, 1), (5, 9)), config, False)
    assert passed[0]["status"] == "PASS"
    failed = core._verdicts(_rows(_row("x", 10, 79.999, 0, 1), (5, 9)), config, False)
    assert failed[0]["status"] == "FAIL"
    few = core._verdicts(_rows(_row("x", 9, 100.0, 0, 1), (5, 9)), config, False)
    assert few[0]["status"] == "too_few_groups"


def test_criterion_2_pass_fail_and_not_separable() -> None:
    config = EvalConfig()
    crit = _row("x", 12, 90.0, 0.5, 2.0)
    assert core._verdicts(_rows(crit, (2.5, 9)), config, False)[1]["status"] == "PASS"
    # CI upper == a rule policy's CI lower: overlapping, never PASS
    assert (
        core._verdicts(_rows(crit, (2.0, 9)), config, False)[1]["status"]
        == "not_separable"
    )
    assert (
        core._verdicts(_rows(_row("x", 12, 90.0, 5.0, 8.0), (1.0, 3.0)), config, False)[
            1
        ]["status"]
        == "FAIL"
    )
    rows = _rows(crit, (2.5, 9))
    rows["random"] = _row("random", 3, 10.0, 2.5, 9)
    assert core._verdicts(rows, config, False)[1]["status"] == "too_few_groups"
    leaked = core._verdicts(_rows(crit, (2.5, 9)), config, True)
    assert [v["status"] for v in leaked] == ["leaked", "leaked"]
    assert [v["would_be"] for v in leaked] == ["PASS", "PASS"]


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
    c1, c2 = report["verdicts"]
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
