"""V0.6 G4 tester round 1 (D108): the estimator against hand-computed values,
the staleness boundary, honesty of every ``source`` (checklist 18) and
determinism (checklist 16). Pure; no database."""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

import pytest

from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    TermSource,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.estimator import estimate
from meshforge.controller.planner.types import (
    ControllerLinkStat,
    EnvBuildStat,
    LinkStat,
)

MB = 1_000_000
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
BIG = "sha256:" + "a" * 64
SMALL = "sha256:" + "b" * 64
EXEC = frozenset({"exec", "exec-subprocess"})
CFG = PlannerConfig(
    default_bw_bytes_s=50 * MB,
    default_compute_s=60.0,
    default_env_s=7.0,
    link_stale_after_s=3600.0,
)


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_slots": 2,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "j",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "k",
        "trees": (TreeNeed(BIG, 200 * MB),),
    }
    base.update(kw)
    return PlanRequest(**base)


def link(bw: float, age_s: float = 10.0) -> LinkStat:
    return LinkStat(bw, 2.0, 4, NOW - dt.timedelta(seconds=age_s))


def snap(*nodes: NodeSnapshot, **kw: Any) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=tuple(nodes), config=CFG, **kw)


def est(req: PlanRequest, n: NodeSnapshot, s: ClusterSnapshot, **kw: Any) -> Any:
    local = kw.pop("local", False)
    now = kw.pop("now", NOW)
    return estimate(req, n, "cpu", s, local=local, now=now)


def test_every_term_summed_by_hand() -> None:
    """transfer 200 MB / 10 MB/s = 20 s; env build 30 s; compute own median
    12 s; output 5 MB over a 5 MB/s controller link = 1 s -> 63 s."""
    target = node("t", cached_envs={})
    holder = node("h", cached=frozenset({BIG}))
    s = snap(
        target,
        holder,
        links={("t", "h"): link(10 * MB)},
        controller_links={"t": ControllerLinkStat(5 * MB, 3)},
        env_builds={"python-cpu": EnvBuildStat(30.0, 2)},
    )
    req = request(
        history=History(
            by_node_s={"t": 12.0},
            samples_by_node={"t": 3},
            output_bytes=5 * MB,
            output_samples=3,
        )
    )
    e = est(req, target, s)
    assert e.queue.seconds == 0
    assert e.transfer.seconds == pytest.approx(20.0)
    assert e.transfer.source is TermSource.MEASURED
    assert e.env.seconds == pytest.approx(30.0) and e.env.source is TermSource.HISTORY
    assert e.compute.seconds == pytest.approx(12.0)
    assert e.compute.source is TermSource.HISTORY
    assert e.output.seconds == pytest.approx(1.0)
    assert e.output.source is TermSource.HISTORY
    assert e.output.detail["symmetric"] is True
    assert e.total_s == pytest.approx(63.0)


def test_nothing_known_is_all_assumed_and_says_so() -> None:
    target = node("t")
    e = est(request(), target, snap(target))
    assert e.transfer.seconds == pytest.approx(200 * MB / (50 * MB))
    assert e.transfer.source is TermSource.ASSUMED
    assert e.env.seconds == 7.0 and e.env.source is TermSource.ASSUMED
    assert e.compute.seconds == 60.0 and e.compute.source is TermSource.ASSUMED
    assert e.output.seconds == 0.0 and e.output.source is TermSource.ASSUMED


@pytest.mark.parametrize(
    ("age", "fresh"),
    [
        (0.0, True),
        (3599.999, True),
        (3600.0, True),
        (3600.001, False),
        (86_400.0, False),
    ],
)
def test_staleness_boundary_is_exactly_one_hour(age: float, fresh: bool) -> None:
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(target, holder, links={("t", "h"): link(10 * MB, age)})
    e = est(request(), target, s)
    if fresh:
        assert e.transfer.source is TermSource.MEASURED
        assert e.transfer.detail["bw_source"] == "probe"
    else:
        assert e.transfer.source is TermSource.ASSUMED
        assert e.transfer.detail["stale_links_ignored"] == ["h"]
        assert e.transfer.seconds == pytest.approx(4.0)  # the default 50 MB/s


def test_without_now_no_link_is_presented_as_measured() -> None:
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(target, holder, links={("t", "h"): link(10 * MB, 1.0)})
    e = est(request(), target, s, now=None)
    assert e.transfer.source is not TermSource.MEASURED


def test_a_link_from_a_node_that_holds_nothing_needed_is_not_used() -> None:
    """The cache index is the only thing that makes a peer a source."""
    target, other = node("t"), node("h", cached=frozenset({"sha256:" + "c" * 64}))
    s = snap(target, other, links={("t", "h"): link(900 * MB)})
    e = est(request(), target, s)
    assert e.transfer.detail["bw_source"] == "assumed"


def test_link_direction_matters() -> None:
    """(src=t, dst=h) is t downloading from h; the reverse row is not t's."""
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(target, holder, links={("h", "t"): link(900 * MB)})
    assert est(request(), target, s).transfer.detail["bw_source"] == "assumed"


def test_a_holder_whose_index_is_gone_gives_no_measured_source() -> None:
    """The snapshot builder empties ``cached`` for an offline holder or a stale
    index; the link must then be worthless."""
    target, holder = node("t"), node("h", cached=frozenset())
    s = snap(target, holder, links={("t", "h"): link(900 * MB)})
    assert est(request(), target, s).transfer.source is TermSource.ASSUMED


def test_seeded_local_pays_no_transfer_for_the_project_only() -> None:
    local = node("l", cached=frozenset())
    req = request(
        trees=(TreeNeed(BIG, 200 * MB), TreeNeed(SMALL, 10 * MB)),
        seeded=True,
        submitter_node_id="l",
    )
    e = est(req, local, snap(local), local=True)
    assert e.transfer.detail["missing_bytes"] == 10 * MB
    assert e.transfer.seconds == pytest.approx(0.2)


def test_the_probe_does_not_cover_trees_the_holder_lacks() -> None:
    """Two missing trees; the only holder has the 1 KB one, over a fast link;
    the 200 MB tree can only come from the controller at 1 MB/s. Presenting
    200 MB over the holder's 100 MB/s link as a *measured* 2 s is wrong by two
    orders of magnitude (the estimator takes the faster of the two sources
    for ALL missing bytes)."""
    target = node("t")
    holder = node("h", cached=frozenset({SMALL}))
    s = snap(
        target,
        holder,
        links={("t", "h"): link(100 * MB)},
        controller_links={"t": ControllerLinkStat(1 * MB, 5)},
    )
    req = request(trees=(TreeNeed(BIG, 200 * MB), TreeNeed(SMALL, 1000)))
    e = est(req, target, s)
    assert e.transfer.seconds >= 200.0, e.transfer


test_the_probe_does_not_cover_trees_the_holder_lacks = pytest.mark.xfail(
    strict=True,
    reason="T-7: transfer time uses the faster of probe/controller for ALL "
    "missing bytes, even those only the controller can serve",
)(test_the_probe_does_not_cover_trees_the_holder_lacks)


def test_scaled_compute_uses_the_score_ratio_and_flags_it() -> None:
    fast = node("f", cpu_score=400.0)
    slow = node("s", cpu_score=100.0)
    req = request(history=History(by_node_s={"s": 8.0}, samples_by_node={"s": 2}))
    e = est(req, fast, snap(fast, slow))
    assert e.compute.source is TermSource.SCALED
    assert e.compute.seconds == pytest.approx(2.0)
    assert e.compute.detail["from_node_id"] == "s"


@pytest.mark.parametrize("mine", [None, 0.0])
def test_missing_or_zero_scores_do_not_divide_by_zero(mine: float | None) -> None:
    a, b = node("a", cpu_score=mine), node("b", cpu_score=100.0)
    req = request(history=History(by_node_s={"b": 8.0}))
    e = est(req, a, snap(a, b))
    assert e.compute.seconds == 8.0 and e.compute.source is TermSource.HISTORY
    assert e.compute.detail["scaled"] is False


def test_gpu_job_scales_by_gpu_score() -> None:
    a = node("a", gpu_score=300.0, cpu_score=1.0)
    b = node("b", gpu_score=100.0, cpu_score=100.0)
    req = request(history=History(by_node_s={"b": 9.0}))
    e = estimate(req, a, "gpu", snap(a, b), local=False, now=NOW)
    assert e.compute.seconds == pytest.approx(3.0)


def test_env_cached_is_free_only_when_the_hash_is_still_there() -> None:
    n = node("n", cached_envs={"h1": 10})
    hit = est(request(history=History(env_hash_by_node={"n": "h1"})), n, snap(n))
    miss = est(request(history=History(env_hash_by_node={"n": "h2"})), n, snap(n))
    assert hit.env.seconds == 0 and hit.env.source is TermSource.MEASURED
    assert miss.env.seconds == 7.0 and miss.env.source is TermSource.ASSUMED


def test_controller_history_is_never_called_measured() -> None:
    n = node("n")
    s = snap(n, controller_links={"n": ControllerLinkStat(20 * MB, 6)})
    e = est(request(), n, s)
    assert e.transfer.seconds == pytest.approx(10.0)
    assert e.transfer.source is TermSource.HISTORY
    assert e.transfer.detail["bw_from"] == "controller"


# ------------------------------------------------------------ honesty + determinism


def random_world(rng: random.Random) -> tuple[ClusterSnapshot, PlanRequest]:
    ids = [f"n{i}" for i in range(rng.randint(2, 5))]
    trees = [TreeNeed(BIG, rng.choice([0, 5 * MB, 300 * MB])), TreeNeed(SMALL, 1000)]
    nodes = [
        node(
            i,
            cpu_score=rng.choice([None, 0.0, 50.0, 400.0]),
            cached=frozenset(t.key for t in trees if rng.random() < 0.4),
            cached_envs={"h": 1} if rng.random() < 0.3 else {},
        )
        for i in ids
    ]
    links = {
        (a, b): link(rng.choice([1e3, 1e6, 1e8]), rng.choice([1, 3600, 4000]))
        for a in ids
        for b in ids
        if a != b and rng.random() < 0.5
    }
    ctl = {
        i: ControllerLinkStat(rng.choice([1e5, 1e7]), 2)
        for i in ids
        if rng.random() < 0.5
    }
    builds = {"python-cpu": EnvBuildStat(20.0, 1)} if rng.random() < 0.5 else {}
    by_node = {i: rng.uniform(1, 50) for i in ids if rng.random() < 0.4}
    req = request(
        trees=tuple(trees),
        history=History(
            by_node_s=by_node,
            output_bytes=rng.choice([None, 1e6]),
            env_hash_by_node={i: "h" for i in ids if rng.random() < 0.5},
        ),
    )
    return snap(*nodes, links=links, controller_links=ctl, env_builds=builds), req


@pytest.mark.parametrize("seed", range(200))
def test_no_term_claims_more_than_its_inputs_support(seed: int) -> None:
    """Checklist 18 on random fabrics: ``measured`` only where something was
    measured (a fresh probe, a cached env, an empty transfer, a slot count);
    ``scaled`` only when the detail says it was; compute and output are never
    ``measured``; a stale link never leaks into a measured transfer."""
    s, req = random_world(random.Random(seed))  # noqa: S311
    decision = plan(req, s, "cost_model", NOW)
    for item in decision.candidates:
        if item.estimate is None:
            continue
        t = item.estimate
        assert t.compute.source is not TermSource.MEASURED
        assert t.output.source is not TermSource.MEASURED
        if t.transfer.source is TermSource.MEASURED:
            assert (
                t.transfer.detail.get("missing_bytes") == 0
                or t.transfer.detail["bw_source"] == "probe"
            )
            if t.transfer.detail.get("bw_source") == "probe":
                assert t.transfer.detail["link_age_s"] <= 3600.0
        if t.transfer.detail.get("bw_source") == "transfer_history":
            assert t.transfer.source is TermSource.HISTORY
        if t.transfer.detail.get("bw_source") == "assumed":
            assert t.transfer.source is TermSource.ASSUMED
        if t.compute.source is TermSource.SCALED:
            assert t.compute.detail["scaled"] is True
        if t.env.source is TermSource.MEASURED:
            assert t.env.detail["env_cached"] is True
        assert t.total_s == pytest.approx(sum(x.seconds for x in t.terms().values()))
        assert all(
            x.seconds >= 0 and x.seconds == x.seconds for x in t.terms().values()
        )
    # the stored JSON carries the same sources (explain prints exactly this)
    for row in candidates_json(decision):
        if row["estimate"]:
            for term in row["estimate"]["terms"].values():
                assert term["source"] in {"measured", "history", "scaled", "assumed"}


@pytest.mark.parametrize("seed", range(40))
def test_determinism_and_node_order_independence(seed: int) -> None:
    s, req = random_world(random.Random(seed))  # noqa: S311
    first = plan(req, s, "cost_model", NOW)
    assert plan(req, s, "cost_model", NOW) == first
    shuffled = ClusterSnapshot(
        nodes=tuple(reversed(s.nodes)),
        config=s.config,
        links=dict(reversed(list(s.links.items()))),
        controller_links=s.controller_links,
        env_builds=s.env_builds,
    )
    other = plan(req, shuffled, "cost_model", NOW)
    assert [c.node_id for c in other.candidates] == [
        c.node_id for c in first.candidates
    ]
    assert (other.chosen and other.chosen.node_id) == (
        first.chosen and first.chosen.node_id
    )
    for a, b in zip(candidates_json(other), candidates_json(first), strict=True):
        assert a["feasible"] == b["feasible"] and a["rank"] == b["rank"]
        if a["estimate"]:
            assert a["estimate"]["total_s"] == b["estimate"]["total_s"]


@pytest.mark.xfail(
    strict=True,
    reason="T-8: equal-bandwidth holders - bw_from depends on the order of "
    "snapshot.nodes (no node_id tie-break in _inbound)",
)
def test_equal_links_name_the_same_holder_whatever_the_snapshot_order() -> None:
    target = node("t")
    h1, h2 = node("h1", cached=frozenset({BIG})), node("h2", cached=frozenset({BIG}))
    links = {("t", "h1"): link(10 * MB), ("t", "h2"): link(10 * MB)}
    froms = {
        est(request(), target, snap(target, *order, links=links)).transfer.detail[
            "bw_from"
        ]
        for order in ((h1, h2), (h2, h1))
    }
    assert len(froms) == 1
