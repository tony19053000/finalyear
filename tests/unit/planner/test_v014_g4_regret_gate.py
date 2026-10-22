"""V0.14 G4 (D319): the regret check of a new calibration version.

* the comparison: exactly x 1.05 the active regret passes, a hair above is
  ``rejected`` with the reason; fewer than 10 groups -> ``skipped
  (too_few_groups)``;
* end to end on hand-built groups: a new version that makes calibrated
  ``cost_model`` pick a slower node in >= 10 groups is rejected; the same
  check on < 10 groups is skipped; a version that improves the picks passes;
  the check is paired (groups counted under both versions).
"""

from __future__ import annotations

import datetime as dt

import pytest
from mf_eval_world import fabric, group, records

from meshforge.controller.planner.calibration import (
    CalibrationConfig,
    MatchedRun,
    TermObservation,
    fit,
)
from meshforge.controller.planner_eval.regret_gate import (
    MIN_GROUPS,
    TOLERANCE,
    regret_check,
    regret_gate,
)

pytestmark = pytest.mark.unit

PREDICTED = {"node_a": 100.0, "node_b": 10.0, "node_c": 30.0, "node_d": 80.0}
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def test_constants_are_d319s() -> None:
    assert (MIN_GROUPS, TOLERANCE) == (10, 0.05)


def test_boundary_exactly_105_percent_passes_and_above_is_rejected() -> None:
    active = 20.0
    limit = active * (1.0 + TOLERANCE)
    passed = regret_gate(limit, active, 10)
    assert passed.status == "passed" and passed.reason is None
    above = regret_gate(limit + 1e-9, active, 10)
    assert above.status == "rejected"
    assert above.reason is not None and "holdout_regret" in above.reason
    assert regret_gate(0.0, 0.0, 12).status == "passed"
    assert regret_gate(0.1, 0.0, 12).status == "rejected"


def test_fewer_than_ten_groups_is_skipped_with_the_reason() -> None:
    check = regret_gate(99.0, 1.0, 9)
    assert check.status == "skipped"
    assert check.reason == "regret check skipped (too_few_groups: 9 < 10)"
    assert check.fit_inputs() == {
        "regret_check": "skipped",
        "regret_groups": 9,
        "holdout_regret_new": 99.0,
        "holdout_regret_active": 1.0,
    }
    assert regret_gate(None, None, 0).status == "skipped"


def _runs(slow_node: str) -> list[MatchedRun]:
    """Runs that measured ``slow_node``'s compute 10x its prediction."""
    out: list[MatchedRun] = []
    n = 0
    for node in ("node_a", "node_b", "node_c", "node_d"):
        ratio = 10.0 if node == slow_node else 1.0
        for _ in range(20):
            n += 1
            out.append(
                MatchedRun(
                    decision_id=n,
                    attempt_id=f"a{n}",
                    node_id=node,
                    finished_at=T0 + dt.timedelta(minutes=n),
                    terms=(
                        TermObservation("compute", 10.0, 10.0 * ratio, fitted=True),
                    ),
                    family="other",
                )
            )
    return out


def _world(count: int) -> tuple[list, list]:  # type: ignore[type-arg]
    """node_b really is the fastest (the prediction is right)."""
    request, snapshot = fabric(PREDICTED)
    groups = [
        group(f"g{i:02d}", {"node_a": 900, "node_b": 100, "node_c": 300, "node_d": 700})
        for i in range(count)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    return groups, recs


def test_a_version_that_worsens_picks_in_ten_groups_is_rejected() -> None:
    groups, recs = _world(10)
    bad = fit(_runs("node_b"), CalibrationConfig())  # wrongly slows node_b
    check = regret_check(groups, recs, bad, None)
    assert check.status == "rejected", check
    assert check.groups == 10
    assert check.regret_active == 0.0 and check.regret_new == 200.0  # picks node_c


def test_the_same_on_nine_groups_is_skipped() -> None:
    groups, recs = _world(9)
    bad = fit(_runs("node_b"), CalibrationConfig())
    check = regret_check(groups, recs, bad, None)
    assert check.status == "skipped" and check.groups == 9
    assert check.reason is not None and "too_few_groups" in check.reason


def test_a_version_that_fixes_picks_passes_against_a_bad_active_one() -> None:
    groups, recs = _world(10)
    bad = fit(_runs("node_b"), CalibrationConfig())
    good = fit(_runs("node_d"), CalibrationConfig())  # harmless: node_d never best
    check = regret_check(groups, recs, good, bad)
    assert check.status == "passed"
    assert check.regret_new == 0.0 and check.regret_active == 200.0
