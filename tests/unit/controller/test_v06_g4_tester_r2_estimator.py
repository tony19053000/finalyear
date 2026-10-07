"""V0.6 G4 tester round 2 - D131 per-tree transfer pricing, attacked again.

R2-3 (strict xfail): one tree named twice in a job (two mounts of the same
dataset, or an input that is the project tree) is moved once; the estimator
bills it twice.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
from test_v06_g4_fix1_resume import MB, NOW, key, node, request

from meshforge.controller.planner import (
    ClusterSnapshot,
    PlannerConfig,
    TermSource,
    TreeNeed,
)
from meshforge.controller.planner.estimator import MAX_TERM_S, estimate
from meshforge.controller.planner.types import ControllerLinkStat, LinkStat

pytestmark = pytest.mark.unit


def test_a_probed_link_exactly_at_the_stale_limit_is_used_one_microsecond_later_is_not() -> (
    None
):
    target, holder = node("t"), node("h", cached=frozenset({key(1)}))
    req = request(trees=(TreeNeed(key(1), MB),))
    for extra, source in ((0.0, TermSource.MEASURED), (1e-6, TermSource.ASSUMED)):
        links = {
            ("t", "h"): LinkStat(1e6, 2.0, 1, NOW - dt.timedelta(seconds=3600 + extra))
        }
        snap = ClusterSnapshot(nodes=(target, holder), links=links)
        assert (
            estimate(req, target, "cpu", snap, local=False, now=NOW).transfer.source
            is source
        )


def test_the_controller_beats_a_slower_probe_per_tree_and_a_probe_beats_the_default() -> (
    None
):
    target, holder = node("t"), node("h", cached=frozenset({key(1)}))
    req = request(trees=(TreeNeed(key(1), 10 * MB),))
    probe = {("t", "h"): LinkStat(1e6, 2.0, 1, NOW)}
    fast_ctl = ClusterSnapshot(
        nodes=(target, holder),
        links=probe,
        controller_links={"t": ControllerLinkStat(50e6, 3, NOW)},
    )
    t = estimate(req, target, "cpu", fast_ctl, local=False, now=NOW).transfer
    assert t.seconds == pytest.approx(0.2) and t.source is TermSource.HISTORY
    no_ctl = ClusterSnapshot(nodes=(target, holder), links=probe)
    t = estimate(req, target, "cpu", no_ctl, local=False, now=NOW).transfer
    assert t.seconds == pytest.approx(10.0) and t.source is TermSource.MEASURED


@pytest.mark.parametrize("size", [-5 * MB, 0, 1, 2**62, 2**63 - 1])
def test_any_database_sized_tree_gives_a_finite_non_negative_term(size: int) -> None:
    target = node("t")
    snap = ClusterSnapshot(nodes=(target,), config=PlannerConfig())
    e = estimate(
        request(trees=(TreeNeed(key(1), size),)),
        target,
        "cpu",
        snap,
        local=False,
        now=NOW,
    )
    assert 0.0 <= e.transfer.seconds <= MAX_TERM_S and math.isfinite(e.total_s)


@pytest.mark.xfail(strict=True, reason="R2-3: a tree named twice is billed twice")
def test_r2_3_the_same_tree_twice_is_billed_once() -> None:
    target = node("t")
    snap = ClusterSnapshot(nodes=(target,))
    once = request(trees=(TreeNeed(key(0), 0), TreeNeed(key(1), 100 * MB)))
    twice = request(
        trees=(
            TreeNeed(key(0), 0),
            TreeNeed(key(1), 100 * MB),
            TreeNeed(key(1), 100 * MB),
        )
    )
    a = estimate(once, target, "cpu", snap, local=False, now=NOW).transfer
    b = estimate(twice, target, "cpu", snap, local=False, now=NOW).transfer
    assert b.seconds == pytest.approx(a.seconds)
    assert b.detail["missing_bytes"] == a.detail["missing_bytes"]
