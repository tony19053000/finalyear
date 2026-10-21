"""V0.14 G3 tester round 2: try to break "pruned == unpruned chosen plan".

Varies what r1/fix-1 did not: policy, objective, fixed vs auto chunks, risk
(reliability + lease release), the link floor, pinned and forced node sets,
hostile numbers, mixed capability/state nodes and seeds beyond 6000.
"""

from __future__ import annotations

import dataclasses
import random
from typing import Any

import pytest
from test_v014_g3_fix1_pruning import _plan_key
from test_v014_g3_tester_r1 import NOW, TREE, _node, _split_fabric, _with_pruning

from meshforge.common.manifest import OBJECTIVES, POLICIES
from meshforge.controller.planner import plan
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    PlanRequest,
    Reliability,
    RiskConfig,
)

pytestmark = pytest.mark.unit

_HOSTILE = (0.0, 1e-9, 1e-3, 1e12, 1e18)


def _varied(seed: int) -> tuple[PlanRequest, ClusterSnapshot, str]:
    rng = random.Random(10_000 + seed)  # noqa: S311
    request, snap = _split_fabric(seed + 20_000)
    ids = [n.node_id for n in snap.nodes]
    nodes = tuple(
        dataclasses.replace(
            n,
            reliability=(
                None
                if rng.random() < 0.4
                else Reliability(
                    failures=rng.randint(0, 5),
                    total=rng.randint(5, 40),
                    window_days=rng.choice((1.0, 7.0, 30.0)),
                )
            ),
        )
        for n in snap.nodes
    )
    cfg = dataclasses.replace(
        snap.config,
        risk=RiskConfig(
            prior=rng.choice(((1.0, 19.0), (0.5, 0.5), (1.0, 1.0))),
            lease_release_s=rng.choice((0.0, 5.0, 60.0, 600.0)),
        ),
        min_useful_bw_bytes_s=rng.choice((0.0, 0.0, 1e6, 1e7, 5e7)),
        min_useful_bytes=rng.choice((0, 64 << 20)),
        prefer_local_margin=rng.choice((0.0, 0.25, 0.9)),
    )
    hist = request.history
    if rng.random() < 0.3:
        hist = dataclasses.replace(
            hist,
            item_rate_by_node={i: rng.choice(_HOSTILE) for i in ids},
            item_rate_samples_by_node=dict.fromkeys(ids, 4),
            chunk_overhead_s=rng.choice(_HOSTILE),
        )
    kw: dict[str, Any] = {
        "objective": rng.choice((None, *OBJECTIVES)),
        "history": hist,
    }
    roll = rng.random()
    if roll < 0.1:
        kw["pinned_node_id"] = rng.choice(ids)
    elif roll < 0.25:
        kw["node_set"] = tuple(sorted(rng.sample(ids, rng.randint(2, len(ids)))))
    if rng.random() < 0.4:
        kw["split"] = dataclasses.replace(
            request.split, chunk_items=rng.choice((None, 1, 2, 7, 10_000))
        )
    return (
        dataclasses.replace(request, **kw),
        dataclasses.replace(snap, nodes=nodes, config=cfg),
        rng.choice(POLICIES),
    )


def test_v014_g3_tester_r2_pruned_equals_unpruned_across_policies_objectives_risk() -> (
    None
):
    skipped = 0
    for seed in range(6500):
        request, snap, policy = _varied(seed)
        try:
            plain = plan(request, _with_pruning(snap, False), policy, NOW)
        except Exception as exc:  # noqa: BLE001
            with pytest.raises(type(exc)):
                plan(request, _with_pruning(snap, True), policy, NOW)
            continue
        cut = plan(request, _with_pruning(snap, True), policy, NOW)
        assert _plan_key(cut) == _plan_key(plain), (seed, policy)
        plain_sets = {(c.mode, c.node_set) for c in plain.candidates}
        for row in cut.candidates:
            # no set is generated only under pruning
            assert (row.mode, row.node_set) in plain_sets, (seed, row.node_set)
        if len(cut.candidates) < len(plain.candidates):
            skipped += 1
    assert skipped > 300, skipped  # not vacuous


def test_v014_g3_tester_r2_pruning_beyond_6000_seeds() -> None:
    skipped = 0
    for seed in range(6000, 12000):
        request, snap = _split_fabric(seed)
        plain = plan(request, _with_pruning(snap, False), "cost_model", NOW)
        cut = plan(request, _with_pruning(snap, True), "cost_model", NOW)
        assert _plan_key(cut) == _plan_key(plain), seed
        skipped += len(cut.candidates) < len(plain.candidates)
    assert skipped > 500


def test_v014_g3_tester_r2_pruning_off_is_deterministic() -> None:
    request, snap, policy = _varied(5)
    a = plan(request, _with_pruning(snap, False), policy, NOW)
    b = plan(request, _with_pruning(snap, False), policy, NOW)
    assert a == b
    c = plan(request, _with_pruning(snap, True), policy, NOW)
    d = plan(request, _with_pruning(snap, True), policy, NOW)
    assert c == d
    assert _node  # imported helper kept for readers
    assert TREE
