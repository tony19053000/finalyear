"""V0.10 G3: the planner's Mode 3 (D265, D275; criterion 5).

Pure: snapshots built by hand. Covered: the C5 "don't split" cases (tiny items,
slow link, one dominating node) and the balanced trio that splits with
K >= 4|S|; forced "don't split" (too few items / nodes, pinned); item-rate
sources (chunk history, run history, scaled, assumed - never shown as
measured); chunk sizing rules; the water-filling start model; prefix sets vs
brute force over every subset (N <= 5); determinism to the byte; the decision
JSON (split rows; rows of a whole job unchanged); the G4 interface
(:class:`SplitPlan`); :func:`split_info`.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import random
from typing import Any

import pytest

from meshforge.common.manifest import Manifest
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    RejectCode,
    SplitConfig,
    SplitInfo,
    TermSource,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json, split_json
from meshforge.controller.planner.inputs import split_info
from meshforge.controller.planner.split import (
    ChunkChoice,
    choose_chunk_items,
    chunk_overhead,
    estimate_split,
    item_rate,
    lane,
    prefix_sets,
)
from meshforge.controller.planner.types import (
    ControllerLinkStat,
    EnvBuildStat,
    LinkStat,
)

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess"})
PROJECT = "sha256:" + "a" * 64
DATA = "sha256:" + "d" * 64
MB = 1_000_000
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_threads": 8,
        "cpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(
    items: int = 3000, history: History | None = None, **kw: Any
) -> PlanRequest:
    split_kw = {
        k: kw.pop(k)
        for k in ("item_bytes", "items_tree", "chunk_items", "min_nodes", "max_nodes")
        if k in kw
    }
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "trees": (TreeNeed(PROJECT, 10 * MB),),
        "submitter_node_id": "n1",
        "seeded": True,
        "history": history or History(),
        "split": SplitInfo(items=items, **split_kw),
    }
    base.update(kw)
    return PlanRequest(**base)


def snap(*nodes: NodeSnapshot, **kw: Any) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=tuple(sorted(nodes, key=lambda n: n.node_id)), **kw)


def trio(**kw: Any) -> ClusterSnapshot:
    return snap(node("n1"), node("n2"), node("n3"), **kw)


def rates(**per_node: float) -> History:
    return History(item_rate_by_node=dict(per_node))


def rows(decision: Any) -> list[dict[str, Any]]:
    return candidates_json(decision)


# ------------------------------------------------------------- criterion 5


def test_c5_balanced_trio_splits_with_four_chunks_per_node() -> None:
    decision = plan(request(history=rates(n1=10, n2=10, n3=10)), trio(), "cost_model")
    assert decision.chosen is not None and decision.chosen.mode == "split"
    chosen = decision.split
    assert chosen is not None and chosen.split
    assert chosen.node_ids == ("n1", "n2", "n3")
    assert chosen.chunks >= 4 * 3
    assert chosen.chunk_items * chosen.chunks >= 3000
    assert chosen.chunk_items * (chosen.chunks - 1) < 3000
    test = chosen.margin
    assert test.passed and test.forced is None
    assert test.set_s is not None and test.threshold_s is not None
    assert test.best_single_s is not None
    assert test.set_s <= test.threshold_s == pytest.approx(0.9 * test.best_single_s)
    # about a third of the single-node time (plus overheads)
    assert test.set_s < 0.45 * test.best_single_s
    assert decision.chosen.estimate is not None
    assert chosen.predicted_makespan_s == pytest.approx(
        decision.chosen.estimate.total_s
    )


def test_c5_tiny_items_do_not_split() -> None:
    """3000 items at 3000 items/s: per-chunk overhead eats any gain."""
    history = rates(n1=3000, n2=3000, n3=3000)
    decision = plan(request(history=history), trio(), "cost_model")
    assert decision.chosen is not None and decision.chosen.mode == "local"
    assert decision.split is not None and not decision.split.split
    assert decision.split.node_ids == ("n1",)
    assert decision.split.chunks == 1 and decision.split.chunk_items == 3000
    test = decision.split.margin
    assert not test.passed and test.forced is None
    assert test.set_s is not None and test.threshold_s is not None
    assert test.set_s > test.threshold_s  # the margin printed says why


def test_c5_slow_link_does_not_split_but_a_fast_one_does() -> None:
    """The items (6 GB) live in the project tree the submitter holds; the
    others would fetch 2 MB per item over 1 MB/s - slower than computing."""
    history = History(
        item_rate_by_node={"n1": 20, "n2": 20, "n3": 20},
    )
    slow = {n: ControllerLinkStat(bw_bytes_s=1 * MB, samples=5) for n in ("n2", "n3")}
    req = request(
        history=history,
        trees=(TreeNeed(PROJECT, 6_010 * MB),),
        item_bytes=6_000 * MB,
        items_tree=PROJECT,
    )
    decision = plan(req, trio(controller_links=slow), "cost_model", NOW)
    assert decision.chosen is not None and decision.chosen.node_id == "n1"
    assert decision.split is not None and not decision.split.split
    assert not decision.split.margin.passed
    fetch = next(
        c for c in rows(decision) if c["node_id"] == "n2" and c["mode"] == "offload"
    )
    terms = fetch["estimate"]["terms"]
    assert terms["items_fetch"]["seconds"] == pytest.approx(3000 * 2.0, rel=1e-6)
    assert terms["items_fetch"]["source"] == "history"  # controller transfer history
    # the same job on a fast LAN splits
    fast = {
        n: ControllerLinkStat(bw_bytes_s=1_000 * MB, samples=5) for n in ("n2", "n3")
    }
    again = plan(req, trio(controller_links=fast), "cost_model", NOW)
    assert again.split is not None and again.split.split


def test_c5_one_dominating_node_does_not_split() -> None:
    decision = plan(request(history=rates(n1=100, n2=1, n3=1)), trio(), "cost_model")
    assert decision.split is not None and not decision.split.split
    assert decision.chosen is not None and decision.chosen.node_id == "n1"
    # a slow node's first chunk outlasts the fast node's whole run: the tail says so
    best_set = next(c for c in rows(decision) if c["mode"] == "split")
    tail = best_set["estimate"]["terms"]["tail"]
    assert tail["seconds"] > 33.0


def test_margin_band_keeps_a_barely_faster_split_unsplit() -> None:
    """A set predicted ~7 % faster stays unsplit at the default margin 0.10
    (noise must not flip it) and splits once the margin is below its gain."""
    history = rates(n1=3000, n2=3000, n3=3000)
    base = trio()
    req = request(items=30_000, history=history)
    seen = plan(req, base, "cost_model")
    assert seen.split is not None and not seen.split.split
    test = seen.split.margin
    assert test.set_s is not None and test.best_single_s is not None
    ratio = test.set_s / test.best_single_s
    assert 0.9 < ratio < 0.99  # faster, but inside the band
    for margin, expect in ((1.0 - ratio - 0.01, True), (1.0 - ratio + 0.01, False)):
        config = PlannerConfig(split=SplitConfig(split_margin=margin))
        out = plan(req, snap(*base.nodes, config=config), "cost_model")
        assert out.split is not None and out.split.split is expect, margin


# ----------------------------------------------------------- forced cases


def test_too_few_items_never_splits_and_says_why() -> None:
    decision = plan(
        request(items=3, history=rates(n1=1, n2=1, n3=1)), trio(), "cost_model"
    )
    assert decision.split is not None and not decision.split.split
    assert decision.split.margin.forced == "split_too_small"
    refused = [c for c in rows(decision) if c["mode"] == "split"]
    assert len(refused) == 1
    assert refused[0]["reject_code"] == RejectCode.SPLIT_TOO_SMALL.value
    assert refused[0]["rank"] is None and refused[0]["node_set"] == ["n1", "n2"]
    # 4 items with min_nodes 2 is enough
    four = plan(
        request(items=4, history=rates(n1=0.01, n2=0.01, n3=0.01)), trio(), "cost_model"
    )
    assert four.split is not None and four.split.margin.forced is None


def test_too_few_nodes_runs_unsplit_on_the_one_that_can() -> None:
    busy = snap(node("n1"), node("n2", used_cpu_slots=1), node("n3", state="offline"))
    decision = plan(request(history=rates(n1=10, n2=10, n3=10)), busy, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "n1"
    assert decision.split is not None and not decision.split.split
    assert decision.split.margin.forced == "split_too_few_nodes"
    refused = [c for c in rows(decision) if c["mode"] == "split"]
    assert [c["reject_code"] for c in refused] == ["split_too_few_nodes"]
    assert not decision.all_static_rejections


def test_min_nodes_three_with_two_nodes_does_not_split() -> None:
    two = snap(node("n1"), node("n2"))
    decision = plan(
        request(history=rates(n1=10, n2=10), min_nodes=3), two, "cost_model"
    )
    assert decision.split is not None and not decision.split.split
    assert decision.split.margin.forced == "split_too_few_nodes"


def test_pinned_split_job_runs_one_lane_on_its_node() -> None:
    decision = plan(
        request(history=rates(n1=10, n2=10, n3=10), pinned_node_id="n2"),
        trio(),
        "cost_model",
    )
    assert [c.node_id for c in decision.candidates] == ["n2"]
    assert decision.split is not None and decision.split.node_ids == ("n2",)
    assert decision.split.margin.forced == "pinned"


def test_no_feasible_node_no_plan_and_static_rule_reads_node_rows_only() -> None:
    dead = snap(
        node("n1", capabilities=frozenset()), node("n2", capabilities=frozenset())
    )
    decision = plan(request(items=2), dead, "cost_model")
    assert decision.chosen is None and decision.split is None
    assert decision.all_static_rejections
    waiting = snap(node("n1", used_cpu_slots=1), node("n2", used_cpu_slots=1))
    decision = plan(request(), waiting, "cost_model")
    assert decision.chosen is None and not decision.all_static_rejections


def test_max_nodes_bounds_the_sets() -> None:
    five = snap(*(node(f"n{i}") for i in range(1, 6)))
    history = rates(**{f"n{i}": 10.0 for i in range(1, 6)})
    decision = plan(request(history=history, max_nodes=3), five, "cost_model")
    sizes = {len(c.node_set) for c in decision.candidates if c.mode == "split"}
    assert sizes and max(sizes) <= 3
    assert decision.split is not None and len(decision.split.lanes) == 3


def test_retry_excluded_node_is_in_no_set() -> None:
    req = request(history=rates(n1=10, n2=10, n3=10), excluded={"n3": "node_lost"})
    decision = plan(req, trio(), "cost_model")
    for c in decision.candidates:
        if c.mode == "split" and c.feasible:
            assert "n3" not in c.node_set
    assert decision.split is not None and "n3" not in decision.split.node_ids


# ----------------------------------------------------------------- sources


def test_rates_come_from_chunk_history_then_run_history_then_scale_then_assume() -> (
    None
):
    nodes = trio()
    cpu = {n.node_id: s for n, s in zip(nodes.nodes, (100.0, 50.0, 25.0), strict=True)}
    scored = snap(
        *(
            NodeSnapshot(**{**_fields(n), "cpu_score": cpu[n.node_id]})
            for n in nodes.nodes
        )
    )
    history = History(
        item_rate_by_node={"n1": 20.0},
        by_node_s={"n2": 300.0},
        samples_by_node={"n2": 4},
        item_rate_samples_by_node={"n1": 7},
    )
    req = request(history=history)
    r1 = item_rate(req, scored.nodes[0], "cpu", scored)
    assert r1.source is TermSource.HISTORY and r1.detail["from"] == "chunk_history"
    assert r1.items_s == pytest.approx(20.0) and r1.detail["samples"] == 7
    r2 = item_rate(req, scored.nodes[1], "cpu", scored)
    assert r2.source is TermSource.HISTORY and r2.detail["from"] == "run_history"
    assert r2.items_s == pytest.approx(3000 / 300.0)
    r3 = item_rate(req, scored.nodes[2], "cpu", scored)
    assert r3.source is TermSource.SCALED and r3.detail["scaled"] is True
    # least work: n1 does 1/20 s x 100 = 5; n2 1/10 x 50 = 5 -> tie on node id: n1
    assert r3.detail["from_node_id"] == "n1"
    assert r3.items_s == pytest.approx(20.0 * 25.0 / 100.0)
    # without scores: the fastest other node's rate as is
    plain = item_rate(req, nodes.nodes[2], "cpu", nodes)
    assert plain.source is TermSource.HISTORY and plain.detail["scaled"] is False
    assert plain.items_s == pytest.approx(20.0)
    # nothing at all: assumed, default_compute_s for the whole job
    none = item_rate(request(), nodes.nodes[0], "cpu", nodes)
    assert none.source is TermSource.ASSUMED
    assert none.items_s == pytest.approx(3000 / PlannerConfig().default_compute_s)


def _fields(n: NodeSnapshot) -> dict[str, Any]:
    return {name: getattr(n, name) for name in NodeSnapshot.__slots__}


def test_assumed_is_never_shown_as_measured() -> None:
    decision = plan(request(), trio(), "cost_model")
    for row in rows(decision):
        if row["estimate"] is None:
            continue
        terms = row["estimate"]["terms"]
        assert terms["compute"]["source"] == "assumed"
        assert terms["chunk_overhead"]["source"] == "assumed"
        assert terms["combine"]["source"] == "assumed"
        if row["mode"] == "split":
            assert terms["tail"]["source"] == "assumed"
        else:
            assert row["rate_source"] == "assumed"


def test_a_set_term_is_as_weak_as_its_weakest_lane() -> None:
    history = History(item_rate_by_node={"n1": 10.0, "n2": 10.0}, chunk_overhead_s=0.8)
    decision = plan(request(history=history), trio(), "cost_model")
    full = next(
        c for c in decision.candidates if c.mode == "split" and len(c.node_set) == 3
    )
    assert full.estimate is not None
    # n3 has no history and no score: its rate is n1's as is (history)
    assert full.estimate.compute.source is TermSource.HISTORY
    assert full.estimate.extra["chunk_overhead"].source is TermSource.HISTORY
    assumed_env = full.estimate.env
    assert assumed_env.source is TermSource.ASSUMED  # default_env_s


def test_overhead_from_history() -> None:
    req = request(history=History(chunk_overhead_s=0.4, chunk_overhead_samples=9))
    term = chunk_overhead(req, SplitConfig())
    assert term.seconds == 0.4 and term.source is TermSource.HISTORY
    assert term.detail == {"samples": 9}
    bad = chunk_overhead(
        request(history=History(chunk_overhead_s=math.nan)), SplitConfig()
    )
    assert bad.source is TermSource.ASSUMED and bad.seconds == 1.5


# ---------------------------------------------------------------- chunking


@pytest.mark.parametrize(
    ("items", "per_item", "lanes", "overhead", "fixed", "expect", "rules"),
    [
        # 20 s at 10 items/s = 200, overhead 1.5 s needs >= 285, balance 3000/12 = 250
        (3000, 0.1, 3, 1.5, None, 250, ("target", "overhead", "balance")),
        # no overhead: the target alone
        (100_000, 0.1, 3, 0.0, None, 200, ("target",)),
        # overhead raises it, balance does not bite
        (100_000, 0.1, 2, 1.5, None, 285, ("target", "overhead")),
        # tiny job: balance wins down to 1 item
        (5, 0.1, 2, 1.5, None, 1, ("target", "balance")),
        # max_chunks: 100 000 items at 1 s each -> target 20 -> 5000 chunks ok;
        # at 100 s each target 1 -> 100 000 chunks > 10 000 -> 10 per chunk
        (100_000, 100.0, 2, 0.0, None, 10, ("target", "max_chunks")),
        # fixed is used as given (capped at the item count)
        (3000, 0.1, 3, 1.5, 7, 7, ("fixed",)),
        (5, 0.1, 3, 1.5, 70, 5, ("fixed",)),
    ],
)
def test_choose_chunk_items(
    items: int,
    per_item: float,
    lanes: int,
    overhead: float,
    fixed: int | None,
    expect: int,
    rules: tuple[str, ...],
) -> None:
    choice = choose_chunk_items(
        items,
        fastest_per_item_s=per_item,
        lanes=lanes,
        overhead_s=overhead,
        config=SplitConfig(),
        fixed=fixed,
    )
    assert choice.items == expect
    assert choice.rules == rules
    assert choice.chunks == math.ceil(items / expect)
    assert choice.chunks <= SplitConfig().max_chunks


def test_chunk_choice_survives_absurd_rates() -> None:
    for per_item in (0.0, -1.0, math.inf, math.nan, 1e-300, 1e300):
        choice = choose_chunk_items(
            1000,
            fastest_per_item_s=per_item,
            lanes=3,
            overhead_s=1.5,
            config=SplitConfig(),
        )
        assert 1 <= choice.items <= 1000 and choice.chunks >= 1


def test_fixed_chunks_are_kept_when_not_splitting() -> None:
    """``chunk: {items: N}`` on one lane runs the user's chunks one after the
    other (the entrypoint may rely on N), paying the overhead per chunk."""
    history = rates(n1=3000, n2=3000, n3=3000)
    req = request(history=history, chunk_items=100, pinned_node_id="n2")
    decision = plan(req, trio(), "cost_model")
    assert decision.split is not None and not decision.split.split
    assert decision.split.chunk_items == 100 and decision.split.chunks == 30
    chosen = decision.chosen
    assert chosen is not None and chosen.estimate is not None
    assert chosen.estimate.extra["chunk_overhead"].seconds == pytest.approx(30 * 1.5)
    # ... which is exactly why a set wins here: it spreads those overheads
    free = plan(request(history=history, chunk_items=100), trio(), "cost_model")
    assert free.split is not None and free.split.split
    assert free.split.chunk_items == 100


# ------------------------------------------------------ the makespan model


def test_terms_add_up_to_the_total() -> None:
    decision = plan(request(history=rates(n1=10, n2=7, n3=3)), trio(), "cost_model")
    for c in decision.candidates:
        if c.estimate is None:
            continue
        total = sum(t.seconds for t in c.estimate.terms().values())
        assert total == pytest.approx(c.estimate.total_s)
        assert set(c.estimate.terms()) == {
            "queue", "transfer", "env", "compute", "output",
            "items_fetch", "chunk_overhead", "tail", "combine",
        }  # fmt: skip


def test_one_lane_is_exactly_compute_plus_one_overhead() -> None:
    req = request(history=rates(n1=10))
    s = trio()
    one = lane(req, s.nodes[0], "cpu", s, local=True)
    est = estimate_split(req, [one], s, ChunkChoice(3000, 1, ("one_chunk",)))
    assert est.estimate.compute.seconds == pytest.approx(300.0)
    assert est.estimate.extra["chunk_overhead"].seconds == pytest.approx(1.5)
    assert est.estimate.extra["tail"].seconds == 0.0
    assert est.estimate.transfer.seconds == 0.0  # seeded Local holds the project


def test_a_lane_that_starts_too_late_gets_no_chunk() -> None:
    """n3 must build a 600 s environment; the others finish before that."""
    history = History(
        item_rate_by_node={"n1": 100.0, "n2": 100.0, "n3": 100.0},
        env_hash_by_node={"n1": "h", "n2": "h"},
    )
    nodes = snap(
        node("n1", cached_envs={"h": 1}),
        node("n2", cached_envs={"h": 1}),
        node("n3"),
        env_builds={"python-cpu": EnvBuildStat(median_s=600.0, samples=3)},
    )
    decision = plan(request(history=history), nodes, "cost_model")
    assert decision.split is not None and decision.split.split
    assert "n3" not in decision.split.node_ids
    full = next(
        c for c in decision.candidates if c.mode == "split" and len(c.node_set) == 3
    )
    inactive = [la.node_id for la in full.lanes if not la.active]
    assert inactive == ["n3"]
    assert next(r for r in rows(decision) if r.get("node_set") == ["n1", "n2", "n3"])[
        "inactive"
    ] == ["n3"]


def test_a_slow_start_still_helps_when_the_job_is_long() -> None:
    history = History(
        item_rate_by_node={"n1": 1.0, "n2": 1.0, "n3": 1.0},
        env_hash_by_node={"n1": "h"},
    )
    nodes = snap(
        node("n1", cached_envs={"h": 1}),
        node("n2"),
        node("n3"),
        env_builds={"python-cpu": EnvBuildStat(median_s=60.0, samples=3)},
    )
    decision = plan(request(history=history), nodes, "cost_model")
    chosen = decision.split
    assert chosen is not None and chosen.node_ids == ("n1", "n2", "n3")
    starts = {la.node_id: la.start_s for la in chosen.lanes}
    assert starts["n1"] == 0.0 and starts["n2"] == pytest.approx(60.2)
    # water filling: n1 works alone until the others are ready, then all three
    eff = chosen.chunk_items / (chosen.chunk_items / 1.0 + 1.5)
    expected_m = (3000 + sum(starts.values()) * eff) / (3 * eff)
    est = decision.chosen.estimate if decision.chosen else None
    assert est is not None
    m = est.transfer.seconds + est.env.seconds + est.compute.seconds
    m += est.extra["items_fetch"].seconds + est.extra["chunk_overhead"].seconds
    assert m == pytest.approx(expected_m, rel=1e-6)
    # D265's max(start) form would charge the 60 s to every lane
    assert m < max(starts.values()) + 3000 / (3 * eff)


def test_project_is_staged_without_the_items() -> None:
    req = request(
        trees=(TreeNeed(PROJECT, 1_010 * MB),),
        item_bytes=1_000 * MB,
        items_tree=PROJECT,
        history=rates(n2=10),
    )
    s = trio()
    remote = lane(req, s.nodes[1], "cpu", s, local=False)
    assert remote.stage.detail["missing_bytes"] == 10 * MB
    assert remote.fetch.detail["bytes_per_item"] == pytest.approx(
        1_000 * MB / 3000, abs=1
    )
    # a node holding the tree fetches nothing per item
    holder = node("n4", cached=frozenset({PROJECT}))
    held = lane(req, holder, "cpu", snap(*s.nodes, holder), local=False)
    assert held.fetch.seconds == 0.0 and held.fetch.detail["held"] is True


def test_items_from_a_peer_use_the_fresh_probe() -> None:
    req = request(
        trees=(TreeNeed(PROJECT, 10 * MB), TreeNeed(DATA, 3_000 * MB)),
        item_bytes=3_000 * MB,
        items_tree=DATA,
        history=rates(n1=1.0),
    )
    holder = node("n1", cached=frozenset({DATA}))
    other = node("n2")
    links = {
        ("n2", "n1"): LinkStat(
            bw_bytes_s=100 * MB, rtt_ms=1.0, samples=3, measured_at=NOW
        )
    }
    s = snap(holder, other, links=links)
    fetch = lane(req, other, "cpu", s, local=False, now=NOW).fetch
    assert fetch.source is TermSource.MEASURED
    assert fetch.seconds == pytest.approx(1 * MB / (100 * MB))
    assert fetch.detail["bw_from"] == "n1"


# --------------------------------------------- prefix sets vs brute force


def _random_case(
    seed: int, *, unequal_starts: bool
) -> tuple[PlanRequest, ClusterSnapshot]:
    rng = random.Random(seed)  # noqa: S311 - test data, not security
    n = rng.randint(2, 5)
    ids = [f"n{i}" for i in range(n)]
    items = rng.choice([8, 50, 300, 3000, 100_000])
    cached = {i for i in ids if rng.random() < 0.5} if unequal_starts else set()
    history = History(
        item_rate_by_node={i: 10 ** rng.uniform(-1, 3) for i in ids},
        chunk_overhead_s=rng.choice([None, 0.0, 0.3, 5.0]),
        env_hash_by_node=dict.fromkeys(cached, "h"),
    )
    nodes = tuple(node(i, cached_envs={"h": 1} if i in cached else {}) for i in ids)
    builds = (
        {"python-cpu": EnvBuildStat(rng.uniform(1, 600), 3)} if unequal_starts else {}
    )
    req = request(items=items, history=history, trees=(), submitter_node_id=None)
    return req, ClusterSnapshot(nodes=nodes, env_builds=builds)


def _best(req: PlanRequest, s: ClusterSnapshot, groups: Any) -> float:
    config = s.config.split
    overhead = chunk_overhead(req, config).seconds
    assert req.split is not None
    best = math.inf
    for group in groups:
        chunk = choose_chunk_items(
            req.split.items,
            fastest_per_item_s=min(g.per_item_s for g in group),
            lanes=len(group),
            overhead_s=overhead,
            config=config,
        )
        best = min(best, estimate_split(req, group, s, chunk).total_s)
    return best


def _lanes(req: PlanRequest, s: ClusterSnapshot) -> list[Any]:
    return [lane(req, n, "cpu", s, local=False) for n in s.nodes]


def test_prefix_sets_are_optimal_when_lanes_start_together() -> None:
    for seed in range(400):
        req, s = _random_case(seed, unequal_starts=False)
        lanes = _lanes(req, s)
        assert req.split is not None
        prefixes = prefix_sets(
            lanes, items=req.split.items, min_nodes=2, max_nodes=None
        )
        everything = [
            g
            for k in range(2, len(lanes) + 1)
            for g in itertools.combinations(lanes, k)
        ]
        assert _best(req, s, prefixes) <= _best(req, s, everything) * (1 + 1e-9), seed


def test_prefix_sets_are_near_optimal_with_unequal_starts() -> None:
    """Brute force over every subset, singles included (what the planner
    really chooses between): within 10 % - the residue is chunk sizing around
    a lane predicted to get no chunk (module docstring of planner.split)."""
    worst = 0.0
    for seed in range(400):
        req, s = _random_case(seed, unequal_starts=True)
        lanes = _lanes(req, s)
        assert req.split is not None
        singles = min(
            estimate_split(req, [one], s, ChunkChoice(req.split.items, 1, ())).total_s
            for one in lanes
        )
        prefixes = prefix_sets(
            lanes, items=req.split.items, min_nodes=2, max_nodes=None
        )
        everything = [
            g
            for k in range(2, len(lanes) + 1)
            for g in itertools.combinations(lanes, k)
        ]
        ours = min(singles, _best(req, s, prefixes))
        brute = min(singles, _best(req, s, everything))
        worst = max(worst, ours / brute - 1)
    assert worst <= 0.10


def test_prefix_sets_are_linear_in_nodes_and_hold_the_fastest() -> None:
    nodes = snap(*(node(f"n{i:02d}") for i in range(20)))
    history = rates(**{f"n{i:02d}": float(i + 1) for i in range(20)})
    req = request(history=history)
    lanes = [lane(req, n, "cpu", nodes, local=False) for n in nodes.nodes]
    sets = prefix_sets(lanes, items=3000, min_nodes=2, max_nodes=None)
    assert len(sets) <= 2 * 19
    assert all(g[0].node_id == "n19" for g in sets)


# ----------------------------------------------------------- determinism


def test_same_input_same_decision_bytes() -> None:
    history = rates(n1=10, n2=7, n3=3)
    a = plan(request(history=history), trio(), "cost_model", NOW)
    shuffled = ClusterSnapshot(nodes=tuple(reversed(trio().nodes)))
    b = plan(request(history=history), shuffled, "cost_model", NOW)

    def dump(decision: Any) -> str:
        return json.dumps(candidates_json(decision), sort_keys=True)

    assert dump(a) == dump(b)
    assert a.split is not None and b.split is not None
    assert json.dumps(split_json(a.split)) == json.dumps(split_json(b.split))
    assert (
        a.snapshot_digest
        == plan(request(history=history), trio(), "cost_model").snapshot_digest
    )


def test_every_policy_plans_a_split_job() -> None:
    from meshforge.common.manifest import POLICIES

    for policy in POLICIES:
        decision = plan(request(history=rates(n1=10, n2=10, n3=10)), trio(), policy)
        assert decision.chosen is not None and decision.split is not None
        assert decision.split.split  # the margin compares the best single by estimate


def test_policy_picks_the_single_and_the_margin_uses_the_best_single() -> None:
    """``least_loaded`` prefers idle n2 over busy-but-fastest n1; the margin
    test still compares the set with the fastest single (n1)."""
    busy = snap(node("n1", cpu_slots=2, used_cpu_slots=1), node("n2"))
    decision = plan(request(history=rates(n1=3000, n2=1500)), busy, "least_loaded")
    assert decision.split is not None and not decision.split.split
    assert decision.chosen is not None and decision.chosen.node_id == "n2"
    assert decision.split.margin.best_single_node_id == "n1"


# ---------------------------------------------------------- decision JSON


WHOLE_KEYS = {
    "node_id", "mode", "feasible", "slot", "reject_code", "reject_text",
    "estimate", "score", "rank",
}  # fmt: skip


def test_rows_of_a_whole_job_are_unchanged() -> None:
    whole = request()
    whole = PlanRequest(
        **{**{k: getattr(whole, k) for k in PlanRequest.__slots__}, "split": None}
    )
    decision = plan(whole, trio(), "cost_model")
    assert decision.split is None
    for row in rows(decision):
        assert set(row) == WHOLE_KEYS
        assert set(row["estimate"]["terms"]) == {
            "queue",
            "transfer",
            "env",
            "compute",
            "output",
        }


def test_split_rows_carry_set_chunks_and_margin() -> None:
    decision = plan(request(history=rates(n1=10, n2=10, n3=10)), trio(), "cost_model")
    out = rows(decision)
    assert out[0]["mode"] == "split" and out[0]["rank"] == 1
    assert out[0]["node_set"] == ["n1", "n2", "n3"]
    assert out[0]["margin_test"]["passed"] is True
    assert out[0]["inactive"] == []
    single = next(r for r in out if r["mode"] == "local")
    assert single["node_set"] == ["n1"] and single["chunks"] == 1
    assert single["rate_items_s"] == 10.0 and single["rate_source"] == "history"
    assert "margin_test" not in single
    json.dumps(out, allow_nan=False)


def test_split_plan_json_is_the_g4_interface() -> None:
    decision = plan(request(history=rates(n1=10, n2=10, n3=10)), trio(), "cost_model")
    assert decision.split is not None
    data = split_json(decision.split)
    assert set(data) == {
        "split", "node_set", "lanes", "chunk_items", "chunks",
        "predicted_makespan_s", "margin_test",
    }  # fmt: skip
    assert [lane["node_id"] for lane in data["lanes"]] == ["n1", "n2", "n3"]
    assert data["lanes"][0]["local"] is True and data["lanes"][1]["local"] is False
    assert all(lane["slot"] == "cpu" for lane in data["lanes"])


# ---------------------------------------------------------------- inputs


def _manifest(over: Any, **split: Any) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "s",
            "entrypoint": ["python", "run.py", "{items_file}", "{out}"],
            "split": {"over": over, **split},
        }
    )


def test_split_info_from_a_manifest() -> None:
    m = _manifest(
        {"glob": "data/imgs/*.jpg"}, chunk={"items": 5}, min_nodes=3, max_nodes=4
    )
    info = split_info(
        m,
        item_count=300,
        item_bytes=3000,
        project_tree=PROJECT,
        input_trees={"data/imgs": DATA},
    )
    assert info == SplitInfo(
        items=300,
        item_bytes=3000,
        items_tree=DATA,
        chunk_items=5,
        min_nodes=3,
        max_nodes=4,
    )
    in_project = split_info(
        _manifest("src/*.txt"), item_count=2, item_bytes=10, project_tree=PROJECT
    )
    assert in_project is not None and in_project.items_tree == PROJECT
    assert in_project.chunk_items is None and in_project.max_nodes is None
    data_less = split_info(
        _manifest({"range": {"stop": 9}}),
        item_count=9,
        item_bytes=None,
        project_tree=PROJECT,
    )
    assert data_less is not None and data_less.items_tree is None
    whole = Manifest.model_validate({"name": "w", "entrypoint": ["true"]})
    assert (
        split_info(whole, item_count=0, item_bytes=None, project_tree=PROJECT) is None
    )
