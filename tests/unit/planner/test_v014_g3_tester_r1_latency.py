"""V0.14 G3 tester round 1: decision latency on fabrics the implementer's
benchmark does not use - heterogeneous lane rates (so dominance pruning and the
water filling do real work), every node with reliability, link floor + pruning
on, each objective, whole and split. Budgets from the brief: p99 < 100 ms (4
nodes), < 500 ms (16). A 32-node run must stay under 2 s (headroom check, not a
brief criterion). Marked ``perf`` like the implementer's file."""

from __future__ import annotations

import dataclasses
import statistics
import time

import pytest
from test_v014_g3_tester_r1 import NOW, _split_fabric

from meshforge.common.manifest import OBJECTIVES
from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import canonical_json, to_replay
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    PlannerConfig,
    PlanRequest,
    Reliability,
)

pytestmark = [pytest.mark.unit, pytest.mark.perf]

RUNS = 60


def _big(n: int, seed: int, *, split: bool) -> tuple[PlanRequest, ClusterSnapshot]:
    # grow the shared builder's fabric to n nodes by cloning its nodes
    request, snap = _split_fabric(seed)
    base = list(snap.nodes)
    nodes = tuple(
        dataclasses.replace(
            base[i % len(base)],
            node_id=f"node_{i:03d}",
            reliability=Reliability(i % 4, 20 + i, 7.0),
        )
        for i in range(n)
    )
    ids = [x.node_id for x in nodes]
    links = {
        (a, b): next(iter(snap.links.values())) for a in ids for b in ids if a != b
    }
    hist = dataclasses.replace(
        request.history,
        by_node_s=dict.fromkeys(ids, 20.0),
        samples_by_node=dict.fromkeys(ids, 4),
        item_rate_by_node={i: 1.0 + (k % 9) * 30.0 for k, i in enumerate(ids)},
        item_rate_samples_by_node=dict.fromkeys(ids, 4),
    )
    request = dataclasses.replace(
        request,
        submitter_node_id=ids[0],
        history=hist,
        split=request.split if split else None,
    )
    config = PlannerConfig(
        min_useful_bw_bytes_s=1e6, prune_dominated=True, split=snap.config.split
    )
    return request, ClusterSnapshot(nodes=nodes, links=links, config=config)


def _p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]


@pytest.mark.parametrize(
    ("nodes", "budget_ms"), [(4, 100.0), (16, 500.0), (32, 2000.0)]
)
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("objective", OBJECTIVES)
def test_v014_g3_tester_r1_decision_latency_budget(
    nodes: int, budget_ms: float, split: bool, objective: str
) -> None:
    ms: list[float] = []
    for run in range(RUNS):
        request, snap = _big(nodes, run % 7, split=split)
        request = dataclasses.replace(request, objective=objective)  # type: ignore[arg-type]
        start = time.perf_counter()
        decision = plan(request, snap, "cost_model", NOW)
        canonical_json(to_replay(request, snap, NOW))
        ms.append((time.perf_counter() - start) * 1000.0)
        assert decision.candidates
    print(  # noqa: T201 - the measurement
        f"\nlatency nodes={nodes} split={split} objective={objective}: "
        f"p50={statistics.median(ms):.1f} ms p99={_p99(ms):.1f} ms"
    )
    assert _p99(ms) < budget_ms
