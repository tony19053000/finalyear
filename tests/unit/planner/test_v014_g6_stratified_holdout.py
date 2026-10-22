"""V0.14 G6 (D329): the D306/D319 held-out slice is stratified per fingerprint.

The G6 harness ran its campaigns one after another; the old newest-overall
slice then held only the last campaign's 3 of 13 groups and the D319 regret
check was skipped. These tests pin the stratified rule.
"""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.controller.planner.calibration import (
    HOLDOUT_FRAC,
    MatchedRun,
    split_holdout,
)

pytestmark = pytest.mark.unit

_T0 = dt.datetime(2026, 10, 22, tzinfo=dt.UTC)


def _run(i: int, fingerprint: str | None) -> MatchedRun:
    return MatchedRun(
        decision_id=i,
        attempt_id=f"a{i}",
        node_id="n1",
        finished_at=_T0 + dt.timedelta(seconds=i),
        terms=(),
        fingerprint=fingerprint,
    )


def _sequential(groups: int, per_group: int) -> list[MatchedRun]:
    """Each group's runs finish before the next group starts (campaigns in turn)."""
    return [
        _run(g * per_group + k, f"fp{g}")
        for g in range(groups)
        for k in range(per_group)
    ]


def test_sequential_campaigns_put_every_group_in_the_slice() -> None:
    runs = _sequential(groups=13, per_group=10)
    fit, held = split_holdout(runs)
    assert {r.fingerprint for r in held} == {f"fp{g}" for g in range(13)}
    assert len(held) == 13 * int(10 * HOLDOUT_FRAC)
    assert len(fit) + len(held) == len(runs)


def test_the_newest_runs_of_each_group_are_held() -> None:
    runs = _sequential(groups=3, per_group=10)
    _, held = split_holdout(runs)
    for g in range(3):
        mine = [r.decision_id for r in held if r.fingerprint == f"fp{g}"]
        assert mine == [g * 10 + 8, g * 10 + 9]


def test_a_small_group_still_gives_one_and_one_off_runs_are_pooled() -> None:
    runs = [
        _run(0, "two"),
        _run(1, "two"),
        _run(2, "one"),
        _run(3, None),
        _run(4, None),
    ]
    fit, held = split_holdout(runs)
    # "two" holds its newest; "one" joins the None pool (2, 3, 4) -> newest 4
    assert [r.decision_id for r in held] == [1, 4]
    assert [r.decision_id for r in fit] == [0, 2, 3]


def test_all_one_off_workloads_behave_as_the_old_newest_slice() -> None:
    runs = [_run(i, f"{i:064x}") for i in range(10)]
    fit, held = split_holdout(runs)
    assert [r.decision_id for r in held] == [8, 9]
    assert len(fit) == 8


def test_both_halves_stay_in_time_order() -> None:
    runs = list(reversed(_sequential(groups=4, per_group=6)))
    fit, held = split_holdout(runs)
    for part in (fit, held):
        assert part == sorted(part, key=lambda r: r.finished_at)


def test_below_min_runs_nothing_is_held() -> None:
    fit, held = split_holdout([_run(0, "x")])
    assert held == [] and len(fit) == 1
