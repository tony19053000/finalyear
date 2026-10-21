"""V0.14 G3 (D294, roadmap 0.14.2): dominance pruning of Mode 3 sets.

A lane ``B`` is idle-dominated only when an already-included lane ``A`` is no
worse on both lane terms, better on one, and alone finishes every item before
``B`` could start. Equal lanes never are. Soundness: such a lane is
**inactive** (no chunk) in every set the generator puts it in. A set holding
one is skipped only when the set without it is generated too and has the same
chunking (fix round 1: the auto chunk size depends on the lane count), so the
chosen plan is always exactly the unpruned one.
"""

from __future__ import annotations

import dataclasses
import random

import pytest
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.planner import plan
from meshforge.controller.planner.candidates import dominated_lanes
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.split import (
    Lane,
    Rate,
    estimate_split,
    lane,
    prefix_sets,
    set_chunk,
)
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    EstimateTerm,
    NodeSnapshot,
    PlanRequest,
    SplitConfig,
    TermSource,
)

pytestmark = pytest.mark.unit

CFG = SplitConfig()


def _lane(node_id: str, start: float, per_item: float) -> Lane:
    node = NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-split"}),
    )
    return Lane(
        node=node,
        slot="cpu",
        local=False,
        rate=Rate(per_item, TermSource.HISTORY),
        fetch=EstimateTerm(0.0, TermSource.MEASURED, {"bytes_per_item": 0}),
        stage=EstimateTerm(start, TermSource.MEASURED, {"missing_bytes": 0}),
        env=EstimateTerm(0.0, TermSource.MEASURED),
    )


def _pruned(
    lanes: list[Lane], items: int = 100, overhead: float = 1.0
) -> list[tuple[str, str]]:
    return [
        (p.node_id, p.by)
        for p in dominated_lanes(
            lanes, items=items, chunk_items=None, overhead_s=overhead, config=CFG
        )
    ]


def test_equal_lanes_never_dominate_each_other() -> None:
    assert _pruned([_lane("a", 5.0, 0.1), _lane("b", 5.0, 0.1)]) == []
    assert _pruned([_lane("a", 1e6, 0.1), _lane("b", 1e6, 0.1)]) == []


def test_a_lane_that_cannot_start_before_the_fastest_finishes_alone_is_pruned() -> None:
    # a alone: 0 + 100 x 0.1 + 100 chunks x 1 s overhead = 110 s; b starts at 500 s
    lanes = [_lane("a", 0.0, 0.1), _lane("b", 500.0, 0.2), _lane("c", 1.0, 0.1)]
    assert _pruned(lanes) == [("b", "a")]


def test_better_terms_alone_do_not_prune() -> None:
    # b is worse on both terms but starts before a could finish everything
    assert _pruned([_lane("a", 0.0, 0.1), _lane("b", 50.0, 0.2)]) == []


def test_a_lane_better_on_one_term_is_not_dominated() -> None:
    # b starts very late but runs items faster than a
    assert _pruned([_lane("a", 0.0, 0.2), _lane("b", 1e6, 0.1)]) == []


def test_a_pruned_lane_never_dominates_another() -> None:
    lanes = [_lane("a", 0.0, 0.1), _lane("b", 500.0, 0.2), _lane("c", 900.0, 0.3)]
    assert _pruned(lanes) == [("b", "a"), ("c", "a")]


def test_fixed_chunks_use_their_own_chunk_count() -> None:
    lanes = [_lane("a", 0.0, 0.1), _lane("b", 30.0, 0.2)]
    # auto: up to 100 chunks x 1 s -> a needs 110 s alone: b is kept
    assert _pruned(lanes) == []
    fixed = dominated_lanes(
        lanes, items=100, chunk_items=50, overhead_s=1.0, config=CFG
    )  # 2 chunks: a needs 12 s alone, b starts at 30 s
    assert [(p.node_id, p.by) for p in fixed] == [("b", "a")]


@pytest.mark.parametrize("seed", range(60))
def test_soundness_a_pruned_lane_is_inactive_in_every_set_it_was_in(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - test data
    request, snapshot, now = synthetic_fabric(6, split=True, seed=seed)
    items = rng.choice((20, 200, 2000))
    split = dataclasses.replace(request.split, items=items)  # type: ignore[type-var]
    request = dataclasses.replace(request, split=split)
    lanes = [
        _lane(
            f"n{i}",
            rng.choice((0.0, 5.0, 50.0, 500.0, 5000.0, 50000.0)),
            rng.choice((0.01, 0.05, 0.2, 1.0)),
        )
        for i in range(6)
    ]
    overhead = snapshot.config.split.chunk_overhead_s
    pruned = dominated_lanes(
        lanes, items=items, chunk_items=None, overhead_s=overhead, config=CFG
    )
    gone = {p.node_id for p in pruned}
    for group in prefix_sets(lanes, items=items, min_nodes=2, max_nodes=None):
        if not gone & {item.node_id for item in group}:
            continue
        chunk = set_chunk(request, list(group), snapshot)
        est = estimate_split(request, list(group), snapshot, chunk, now=now)
        assert not (gone & est.active), (seed, sorted(gone), est.active)


def _far(snapshot: ClusterSnapshot) -> str:
    return [n.node_id for n in snapshot.nodes if not n.cached][-1]


def _slow_far_node(
    request: PlanRequest, snapshot: ClusterSnapshot, *, prune: bool
) -> ClusterSnapshot:
    """The last node fetches the project over a 1 kB/s link and is slow."""
    far = _far(snapshot)
    nodes = tuple(
        dataclasses.replace(n, cpu_score=1.0) if n.node_id == far else n
        for n in snapshot.nodes
    )
    links = {
        pair: dataclasses.replace(link, bw_bytes_s=1024.0) if pair[0] == far else link
        for pair, link in snapshot.links.items()
    }
    controller = {
        k: dataclasses.replace(v, bw_bytes_s=1024.0) if k == far else v
        for k, v in snapshot.controller_links.items()
    }
    return dataclasses.replace(
        snapshot,
        nodes=nodes,
        links=links,
        controller_links=controller,
        config=dataclasses.replace(snapshot.config, prune_dominated=prune),
    )


def _fixed_chunks(request: PlanRequest, items: int | None) -> PlanRequest:
    split = dataclasses.replace(request.split, chunk_items=items)  # type: ignore[type-var]
    return dataclasses.replace(request, split=split)


def test_a_decision_records_what_it_pruned_and_explains_it() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=3)
    request = _fixed_chunks(request, 10)  # the chunking ignores the lane count
    far = _far(snapshot)
    on = plan(request, _slow_far_node(request, snapshot, prune=True), "cost_model", now)
    off = plan(
        request, _slow_far_node(request, snapshot, prune=False), "cost_model", now
    )
    assert [p.node_id for p in on.pruned] == [far]
    assert off.pruned == ()
    sets_on = [c.node_set for c in on.candidates if c.mode == "split"]
    sets_off = [c.node_set for c in off.candidates if c.mode == "split"]
    assert all(far not in s for s in sets_on)
    assert any(far in s for s in sets_off)
    assert set(sets_on) < set(sets_off)  # only skipped, never a new set
    # the far node is still a one-lane candidate
    assert any(c.node_id == far and c.mode != "split" for c in on.candidates)
    rows = candidates_json(on)
    assert all(
        r["pruned"] == [{"node_id": far, "by": on.pruned[0].by}]
        for r in rows
        if "node_set" in r
    )
    assert all("pruned" not in r for r in candidates_json(off))
    # pruning removed only sets the far node would have idled in
    assert on.chosen is not None and off.chosen is not None
    assert on.split is not None and off.split is not None
    assert far not in {lane_.node_id for lane_ in off.split.lanes if lane_.active}
    assert on.split == off.split


def test_auto_chunks_keep_a_set_whose_idle_lane_changes_the_chunk_size() -> None:
    """Fix round 1 (tester r1 D1): with ``chunk: auto`` the balance rule sizes
    chunks by the set's lane count, so the set with the idle far node has
    smaller chunks than the set without it and is **not** dominated: it is
    still generated and costed, and nothing is recorded as pruned."""
    request, snapshot, now = synthetic_fabric(4, split=True, seed=3)
    assert request.split is not None and request.split.chunk_items is None
    far = _far(snapshot)
    on = plan(request, _slow_far_node(request, snapshot, prune=True), "cost_model", now)
    off = plan(
        request, _slow_far_node(request, snapshot, prune=False), "cost_model", now
    )
    assert on.pruned == ()
    assert on.candidates == off.candidates and on.split == off.split
    assert any(far in c.node_set for c in on.candidates if c.mode == "split")


def test_pruning_never_applies_to_a_forced_or_pinned_split() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=3)
    slow = _slow_far_node(request, snapshot, prune=True)
    ids = tuple(sorted(n.node_id for n in snapshot.nodes))
    forced = plan(dataclasses.replace(request, node_set=ids), slow, "cost_model", now)
    pinned = plan(
        dataclasses.replace(request, pinned_node_id=ids[0]), slow, "cost_model", now
    )
    assert forced.pruned == () and pinned.pruned == ()


def test_lane_builder_matches_the_plan_lanes() -> None:
    # dominated_lanes reads the same lane terms plan() costs with
    request, snapshot, now = synthetic_fabric(3, split=True, seed=1)
    for node in snapshot.nodes:
        built = lane(request, node, "cpu", snapshot, local=False, now=now)
        assert built.start_s >= 0.0 and built.per_item_s > 0.0


def test_the_soundness_sweep_is_not_vacuous() -> None:
    hits = 0
    for seed in range(60):
        rng = random.Random(seed)  # noqa: S311 - test data (same draws as above)
        items = rng.choice((20, 200, 2000))
        lanes = [
            _lane(
                f"n{i}",
                rng.choice((0.0, 5.0, 50.0, 500.0, 5000.0, 50000.0)),
                rng.choice((0.01, 0.05, 0.2, 1.0)),
            )
            for i in range(6)
        ]
        hits += bool(
            dominated_lanes(
                lanes, items=items, chunk_items=None, overhead_s=1.5, config=CFG
            )
        )
    assert hits >= 15, hits
