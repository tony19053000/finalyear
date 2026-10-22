"""V0.14 G4 tester round 2 - D322 paired criterion 2, T1/T2/T3 fixes.

Derived from brief 5d (D319, D322), not from the implementation: an
independent re-implementation of the paired verdict is compared with
``evaluate`` on random worlds; the exact x1.05 boundary is swept over decimals
with ``Fraction``; the 15 % within-check is swept at the reported precision;
non-finite medians never reach the JSON.
"""

from __future__ import annotations

import json
import math
import random
from fractions import Fraction
from typing import Any

import pytest
from mf_eval_world import fabric, group, records

from meshforge.bench.stats import bootstrap_mean_ci
from meshforge.controller.planner_eval.core import _verdicts, evaluate, group_regret
from meshforge.controller.planner_eval.model import (
    CRITERION_POLICY,
    RULE_POLICIES,
    EvalConfig,
    default_policies,
)
from meshforge.controller.planner_eval.regret_gate import regret_gate
from meshforge.controller.planner_eval.report import render_markdown, report_text

pytestmark = pytest.mark.unit

COST = CRITERION_POLICY.key
CFG = EvalConfig(bootstrap_resamples=300)


def _rows(n_crit: int, unmeasured: dict[str, int] | None = None) -> dict[str, Any]:
    un = unmeasured or {}
    rows: dict[str, Any] = {
        COST: {
            "key": COST,
            "counted_groups": n_crit,
            "within_threshold_pct": 100.0,
            "unmeasured_groups": un.get(COST, 0),
        }
    }
    for p in RULE_POLICIES:
        rows[p] = {
            "key": p,
            "counted_groups": 50,
            "within_threshold_pct": 0.0,
            "unmeasured_groups": un.get(p, 0),
        }
    return rows


def _groups(n: int, prefix: str = "g") -> list[tuple[str, str]]:
    return [("camp_1", f"{prefix}{i:03d}") for i in range(n)]


def _expected(crit: dict[Any, float], rule: dict[str, dict[Any, float]]) -> Any:
    """Independent statement of D322."""
    lows: list[float] = []
    highs: list[float] = []
    for p in RULE_POLICIES:
        common = sorted(crit.keys() & rule[p].keys())
        if len(common) < 10:
            return "too_few_groups", None
        d = [round(rule[p][g] - crit[g], 6) for g in common]
        ci = bootstrap_mean_ci(
            d,
            seed=CFG.bootstrap_seed,
            resamples=CFG.bootstrap_resamples,
            level=CFG.ci_level,
        )
        lows.append(round(ci.low, 6))
        highs.append(round(ci.high, 6))
    if all(v > 0 for v in lows):
        return "PASS", min(lows)
    if any(h < 0 for h in highs):
        return "FAIL", min(lows)
    return "not_separable", min(lows)


# ------------------------------------------------------------ D322 semantics


@pytest.mark.parametrize("seed", range(60))
def test_paired_verdict_matches_an_independent_statement(seed: int) -> None:
    rnd = random.Random(seed)  # noqa: S311
    n = rnd.randint(8, 30)
    gs = _groups(n)
    crit = {g: rnd.choice([0.0, 0.0, 5.0, 20.0, 50.0]) for g in gs}
    shift = rnd.choice([-30.0, -2.0, 0.0, 3.0, 40.0, 200.0])
    rule: dict[str, dict[Any, float]] = {}
    for p in RULE_POLICIES:
        # each policy counts a random subset (selection), differing per policy
        keep = [g for g in gs if rnd.random() < 0.9]
        rule[p] = {g: max(0.0, crit[g] + shift + rnd.uniform(-8, 8)) for g in keep}
    regrets = {COST: crit, **rule}
    by_n = {COST: len(crit), **{p: len(r) for p, r in rule.items()}}
    rows = _rows(len(crit))
    for p in RULE_POLICIES:
        rows[p]["counted_groups"] = by_n[p]
    out = _verdicts(rows, regrets, CFG, leaked=False)
    v = next(x for x in out if x["criterion"] == 2)
    status, floor = _expected(crit, rule)
    assert v["status"] == status, v
    assert v["threshold"] == 0.0
    if floor is not None:
        assert v["value"] == pytest.approx(floor, abs=1e-6)
    assert len(v["comparisons"]) == len(RULE_POLICIES)
    for c in v["comparisons"]:
        common = crit.keys() & rule[c["key"]].keys()
        assert c["common_groups"] == len(common)
        if len(common) >= 10:
            d = [round(rule[c["key"]][g] - crit[g], 6) for g in sorted(common)]
            assert c["mean_diff_pct"] == pytest.approx(sum(d) / len(d), abs=1e-5)
            assert c["ci_low"] <= c["mean_diff_pct"] <= c["ci_high"]
        else:
            assert c["ci_low"] is None and c["ci_high"] is None


def test_pairing_uses_only_common_groups_not_each_policys_own_counted_set() -> None:
    """crit is good everywhere (0) but missing the 10 hard groups; every rule
    policy is bad (50) on easy groups only... so unpaired they differ from
    paired. Pairing must look only at the 10 shared easy groups."""
    easy, hard = _groups(10, "e"), _groups(10, "h")
    crit = dict.fromkeys(easy, 0.0)
    rule = {
        p: {**dict.fromkeys(easy, 30.0), **dict.fromkeys(hard, 0.0)}
        for p in RULE_POLICIES
    }
    rows = _rows(10)
    out = _verdicts(rows, {COST: crit, **rule}, CFG, False)
    v = next(x for x in out if x["criterion"] == 2)
    assert v["status"] == "PASS" and v["value"] == pytest.approx(30.0)
    assert all(
        c["common_groups"] == 10 and c["mean_diff_pct"] == 30.0
        for c in v["comparisons"]
    )
    assert v["counted_groups"] == 10


def test_nine_common_groups_is_too_few_even_if_each_policy_counts_many() -> None:
    gs = _groups(20)
    crit = dict.fromkeys(gs[:9], 0.0) | dict.fromkeys(gs[9:19], 0.0)
    rule = {
        p: dict.fromkeys(gs[:9], 99.0) | {("camp_1", f"z{i}"): 0.0 for i in range(20)}
        for p in RULE_POLICIES
    }
    out = _verdicts(_rows(len(crit)), {COST: crit, **rule}, CFG, False)
    v = next(x for x in out if x["criterion"] == 2)
    assert v["status"] == "too_few_groups"
    assert v["counted_groups"] == 9 and v["value"] is None


def test_exactly_ten_common_groups_is_enough() -> None:
    gs = _groups(10)
    crit = dict.fromkeys(gs, 0.0)
    rule = {p: dict.fromkeys(gs, 10.0) for p in RULE_POLICIES}
    out = _verdicts(_rows(10), {COST: crit, **rule}, CFG, False)
    assert next(x for x in out if x["criterion"] == 2)["status"] == "PASS"


def test_identical_regrets_are_not_separable_never_pass() -> None:
    gs = _groups(15)
    crit = dict.fromkeys(gs, 7.0)
    out = _verdicts(
        _rows(15), {COST: crit, **{p: dict(crit) for p in RULE_POLICIES}}, CFG, False
    )
    v = next(x for x in out if x["criterion"] == 2)
    assert v["status"] == "not_separable"  # CI [0,0]: lower bound 0 is not > 0
    assert v["value"] == 0.0


def test_one_policy_tied_blocks_pass_one_policy_better_gives_fail() -> None:
    gs = _groups(12)
    crit = dict.fromkeys(gs, 10.0)
    rule = {p: dict.fromkeys(gs, 25.0) for p in RULE_POLICIES}
    rule[RULE_POLICIES[0]] = dict(crit)
    out = _verdicts(_rows(12), {COST: crit, **rule}, CFG, False)
    assert next(x for x in out if x["criterion"] == 2)["status"] == "not_separable"
    rule[RULE_POLICIES[1]] = dict.fromkeys(gs, 1.0)  # clearly better than cost
    out = _verdicts(_rows(12), {COST: crit, **rule}, CFG, False)
    v = next(x for x in out if x["criterion"] == 2)
    assert v["status"] == "FAIL" and RULE_POLICIES[1] in v["detail"]
    assert v["value"] == pytest.approx(-9.0)  # lowest lower bound


def test_leaked_stamps_criterion_2_but_keeps_comparisons() -> None:
    gs = _groups(12)
    crit = dict.fromkeys(gs, 0.0)
    out = _verdicts(
        _rows(12),
        {COST: crit, **{p: dict.fromkeys(gs, 9.0) for p in RULE_POLICIES}},
        CFG,
        True,
    )
    v = next(x for x in out if x["criterion"] == 2)
    assert v["status"] == "leaked" and v["would_be"] == "PASS"
    assert len(v["comparisons"]) == len(RULE_POLICIES)


def test_missing_criterion_policy_row_is_too_few_not_a_crash() -> None:
    rows = _rows(0)
    del rows[COST]
    out = _verdicts(rows, {}, CFG, False)
    assert [v["status"] for v in out] == ["too_few_groups", "too_few_groups"]


def test_detail_names_the_criterion_policys_unmeasured_count() -> None:
    gs = _groups(12)
    crit = dict.fromkeys(gs, 0.0)
    out = _verdicts(
        _rows(12, {COST: 7}),
        {COST: crit, **{p: dict.fromkeys(gs, 9.0) for p in RULE_POLICIES}},
        CFG,
        False,
    )
    assert (
        "unmeasured in 7 group" in next(x for x in out if x["criterion"] == 2)["detail"]
    )


def test_end_to_end_report_json_has_comparisons_and_is_valid_json() -> None:
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0, "node_c": 50.0})
    gs = [
        group(f"g{i:02d}", {"node_a": 900, "node_b": 100, "node_c": 300})
        for i in range(12)
    ]
    recs = [r for g in gs for r in records(g, request, snapshot, reps=1)]
    report = evaluate(gs, recs, {}, default_policies(), CFG, campaign_ids=["camp_1"])
    text = report_text(report)
    back = json.loads(text)
    v2 = next(v for v in back["verdicts"] if v["criterion"] == 2)
    assert {c["key"] for c in v2["comparisons"]} == set(RULE_POLICIES)
    assert all(c["common_groups"] <= 12 for c in v2["comparisons"])
    render_markdown(report)
    assert text == report_text(report)  # deterministic


# ------------------------------------------------------------ T1 gate boundary


def _exact_gate(new: Fraction, active: Fraction) -> bool:
    return new <= active * Fraction(21, 20)


def test_regret_gate_matches_exact_rational_arithmetic_over_decimals() -> None:
    """Every pair of 1-4 decimal values, around the boundary: the gate agrees
    with exact arithmetic on the shortest decimal repr (D319 'exactly x1.05
    accepted')."""
    rnd = random.Random(7)  # noqa: S311
    bad: list[tuple[float, float]] = []
    checked = 0
    for decimals in (1, 2, 3, 4, 5):
        scale = 10**decimals
        for _ in range(4000):
            active_i = rnd.randint(1, 40 * scale)
            active = active_i / scale
            exact_up = Fraction(str(active)) * Fraction(21, 20)
            for delta in (-1, 0, 1):
                new_f = float(exact_up) + delta * 10 ** -(decimals + 3)
                new = round(new_f, decimals + 3)
                want = _exact_gate(Fraction(str(new)), Fraction(str(active)))
                got = regret_gate(new, active, 10).status == "passed"
                checked += 1
                if want != got:
                    bad.append((new, active))
    assert not bad, bad[:10]
    assert checked > 50_000


@pytest.mark.parametrize(
    ("new", "active"),
    [
        (1.197, 1.14),
        (21.0, 20.0),
        (0.105, 0.1),
        (1.05, 1.0),
        (10.5, 10.0),
        (0.00105, 0.001),
    ],
)
def test_exact_boundary_accepted_and_one_ulp_above_rejected(
    new: float, active: float
) -> None:
    assert regret_gate(new, active, 10).status == "passed"
    above = math.nextafter(new, math.inf)
    assert regret_gate(above, active, 10).status == "rejected"


def test_gate_zero_active_and_edge_values() -> None:
    assert regret_gate(0.0, 0.0, 10).status == "passed"
    assert regret_gate(0.0001, 0.0, 10).status == "rejected"
    assert regret_gate(None, 1.0, 10).status == "skipped"
    assert regret_gate(1.0, None, 10).status == "skipped"
    assert regret_gate(1.0, 1.0, 9).status == "skipped"
    assert regret_gate(1e300, 1e299, 10).status == "rejected"
    assert regret_gate(1e-300, 1e-299, 10).status == "passed"


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(
            float("nan"),
            marks=pytest.mark.xfail(
                strict=True,
                raises=pytest.fail.Exception,
                reason="DEFECT T4 (minor): regret_gate(nan, x) raises decimal.InvalidOperation "
                "(pre-fix float code rejected); unreachable while T3 keeps NaN out of the means",
            ),
        ),
        float("inf"),
    ],
)
def test_gate_non_finite_does_not_crash_or_pass_silently(bad: float) -> None:
    for a, b in ((bad, 1.0), (1.0, bad)):
        try:
            res = regret_gate(a, b, 10)
        except Exception as exc:  # noqa: BLE001 - recorded, see assertion
            pytest.fail(f"regret_gate({a}, {b}) raised {exc!r}")
        assert res.status in {"rejected", "skipped"} or (
            a == 1.0 and b == bad and res.status == "passed"
        )


# ------------------------------------------------------------ T2 within 15 %


def test_group_regret_is_rounded_to_the_reported_precision() -> None:
    rnd = random.Random(3)  # noqa: S311
    for _ in range(3000):
        best = rnd.randint(1, 50_000) / rnd.choice([1, 7, 10, 100])
        g = group("g1", {"node_a": best * 1.15, "node_b": best})
        cfg = next(c for c in g.configs if c.node_id == "node_a")
        reg = group_regret(g, cfg.config_id, best)
        assert reg is not None
        pct, ms = reg
        assert pct == round(pct, 6) and ms == round(ms, 6)


@pytest.mark.parametrize("best", [14.714285714285714, 100.0, 7.0, 0.3, 123.456, 1e-3])
def test_pick_at_115_percent_counts_as_within(best: float) -> None:
    """The reported regret is (rounded to 6 decimals) 15.0 -> within."""
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0})
    gs = [
        group(f"g{i:02d}", {"node_a": best * 1.15, "node_b": best}) for i in range(10)
    ]
    # Local (node_a) is the rule policy least_loaded's pick; cost picks node_b.
    recs = [r for g in gs for r in records(g, request, snapshot, reps=1)]
    report = evaluate(gs, recs, {}, default_policies(), CFG, campaign_ids=["c"])
    (row,) = [r for r in report["policies"] if r["key"] == "least_loaded"]
    assert row["mean_regret_pct"] == pytest.approx(15.0, abs=1e-4)
    assert row["within_threshold_pct"] == 100.0, row


def test_pick_clearly_over_15_percent_is_not_within() -> None:
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0})
    gs = [group(f"g{i:02d}", {"node_a": 116.0, "node_b": 100.0}) for i in range(10)]
    recs = [r for g in gs for r in records(g, request, snapshot, reps=1)]
    report = evaluate(gs, recs, {}, default_policies(), CFG, campaign_ids=["c"])
    (row,) = [r for r in report["policies"] if r["key"] == "least_loaded"]
    assert row["within_threshold_pct"] == 0.0


# ------------------------------------------------------------ T3 non-finite


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("where", ["best", "other", "all"])
def test_non_finite_median_group_excluded_counted_and_json_valid(
    bad: float, where: str
) -> None:
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0, "node_c": 50.0})
    normal = group("g_ok", {"node_a": 900, "node_b": 100, "node_c": 300})
    meds = {
        "best": {"node_a": 900.0, "node_b": bad, "node_c": 300.0},
        "other": {"node_a": bad, "node_b": 100.0, "node_c": 300.0},
        "all": {"node_a": bad, "node_b": bad, "node_c": bad},
    }[where]
    odd = group("g_bad", meds)
    gs = [normal, odd]
    recs = [r for g in gs for r in records(g, request, snapshot, reps=1)]
    report = evaluate(gs, recs, {}, default_policies(), CFG, campaign_ids=["c"])
    text = report_text(report)
    back = json.loads(text, parse_constant=lambda c: pytest.fail(f"bare {c} in JSON"))
    assert back["counts"]["non_finite_configs"] >= 1
    bad_group = next(g for g in back["groups"] if g["group_id"] == "g_bad")
    assert bad_group["status"] == "excluded"
    ok_group = next(g for g in back["groups"] if g["group_id"] == "g_ok")
    assert ok_group["status"] == "evaluated"
    assert "NaN" not in text and "Infinity" not in text
    render_markdown(report)


def test_non_finite_counts_every_bad_config_across_groups() -> None:
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0, "node_c": 50.0})
    gs = [
        group(f"g{i}", {"node_a": float("nan"), "node_b": float("inf"), "node_c": 5.0})
        for i in range(3)
    ]
    recs = [r for g in gs for r in records(g, request, snapshot, reps=1)]
    report = evaluate(gs, recs, {}, default_policies(), CFG, campaign_ids=["c"])
    assert report["counts"]["non_finite_configs"] == 6
    json.loads(report_text(report))
