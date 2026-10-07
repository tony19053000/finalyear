"""V0.6 G4 tester round 2 - D131 per-tree transfer pricing, attacked again.

R2-3 (fixed): one tree named twice in a job (two mounts of the same dataset,
or an input that is the project tree) is moved once, and the estimator,
feasibility.missing_bytes and cache_affinity count it once.
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
from meshforge.controller.planner.feasibility import missing_bytes, missing_trees
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


def test_r2_3_feasibility_missing_bytes_counts_a_repeated_tree_once() -> None:
    target = node("t")
    trees = (TreeNeed(key(0), 7), TreeNeed(key(1), 100 * MB))
    once = request(trees=trees)
    twice = request(trees=(*trees, TreeNeed(key(1), 100 * MB), TreeNeed(key(0), 7)))
    assert missing_bytes(twice, target, local=False) == 7 + 100 * MB
    assert missing_bytes(twice, target, local=False) == missing_bytes(
        once, target, local=False
    )
    assert [t.key for t in missing_trees(twice, target, local=False)] == [
        key(0),
        key(1),
    ]


def test_r2_3_distinct_trees_are_still_billed_each() -> None:
    """Dedupe is by tree id only: two different trees of one size both move."""
    target = node("t")
    one = request(trees=(TreeNeed(key(0), 0), TreeNeed(key(1), 100 * MB)))
    two = request(
        trees=(
            TreeNeed(key(0), 0),
            TreeNeed(key(1), 100 * MB),
            TreeNeed(key(2), 100 * MB),
        )
    )
    snap = ClusterSnapshot(nodes=(target,))
    a = estimate(one, target, "cpu", snap, local=False, now=NOW).transfer
    b = estimate(two, target, "cpu", snap, local=False, now=NOW).transfer
    assert b.detail["missing_bytes"] == 200 * MB
    assert b.seconds == pytest.approx(2 * a.seconds)
    assert missing_bytes(two, target, local=False) == 200 * MB


def test_r2_3_an_input_that_is_the_project_tree_is_held_on_a_seeded_local() -> None:
    """D109: the seeded Local candidate holds the project tree - also where an
    input names it again; a remote node moves it once."""
    target = node("t")
    req = request(
        trees=(TreeNeed(key(0), 50 * MB), TreeNeed(key(0), 50 * MB)), seeded=True
    )
    assert missing_bytes(req, target, local=True) == 0
    assert missing_bytes(req, target, local=False) == 50 * MB
    snap = ClusterSnapshot(nodes=(target,))
    local = estimate(req, target, "cpu", snap, local=True, now=NOW).transfer
    remote = estimate(req, target, "cpu", snap, local=False, now=NOW).transfer
    assert local.seconds == 0.0 and local.detail["missing_bytes"] == 0
    assert remote.detail["missing_bytes"] == 50 * MB


def test_r2_3_per_tree_items_list_a_repeated_tree_once() -> None:
    """D131 per-tree detail: one item per distinct missing tree."""
    target, holder = node("t"), node("h", cached=frozenset({key(1)}))
    snap = ClusterSnapshot(
        nodes=(target, holder),
        links={("t", "h"): LinkStat(10 * MB, 2.0, 1, NOW)},
    )
    req = request(
        trees=(
            TreeNeed(key(0), 0),
            TreeNeed(key(1), 10 * MB),
            TreeNeed(key(2), 10 * MB),
            TreeNeed(key(1), 10 * MB),
        )
    )
    term = estimate(req, target, "cpu", snap, local=False, now=NOW).transfer
    assert term.detail["missing_bytes"] == 20 * MB
    items = term.detail.get("items")
    assert items is not None
    assert [i["tree"] for i in items] == [key(0), key(1), key(2)]


def test_r2_3_cache_affinity_counts_a_repeated_held_tree_once() -> None:
    from meshforge.controller.planner.policies import Scored, _cached_bytes

    holder = node("h", cached=frozenset({key(1)}))
    once = request(trees=(TreeNeed(key(0), 0), TreeNeed(key(1), 100 * MB)))
    twice = request(
        trees=(
            TreeNeed(key(0), 0),
            TreeNeed(key(1), 100 * MB),
            TreeNeed(key(1), 100 * MB),
        )
    )
    snap = ClusterSnapshot(nodes=(holder,))
    est = estimate(twice, holder, "cpu", snap, local=False, now=NOW)
    item = Scored(node=holder, slot="cpu", local=False, estimate=est)
    assert _cached_bytes(twice, item) == _cached_bytes(once, item) == 100 * MB
