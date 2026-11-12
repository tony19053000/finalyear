"""Tester pass, round G11 (D456): adversarial inputs for the per-node chunk cost.

Derived from the spec (docs/briefs/progress/V1.0-G11-lab-fixes.md, D456), not
from the implementation: order of samples never matters (determinism), hostile
numbers never reach the cost model, and a node without enough samples falls
back to the shared overhead.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from typing import Any

import pytest

from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.chunk_cost import (
    ChunkSample,
    chunk_costs,
    node_chunk_cost,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.types import PlannerConfig

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess", "exec-split"})
PROJECT = "sha256:" + "a" * 64
ENV = "e" * 64
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.UTC)


def _node(node_id: str) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=EXEC,
        cpu_threads=8,
        cpu_slots=1,
        cached=frozenset({PROJECT}),
        cached_envs={ENV: 1},
    )


def _snap(*nodes: NodeSnapshot) -> ClusterSnapshot:
    return ClusterSnapshot(
        nodes=tuple(sorted(nodes, key=lambda n: n.node_id)),
        config=PlannerConfig(split=SplitConfig()),
    )


def _request(history: History, items: int = 400) -> PlanRequest:
    return PlanRequest(
        job_id="job_01J0000000000000000000000",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key=ENV,
        trees=(TreeNeed(PROJECT, 1000),),
        submitter_node_id="n1",
        seeded=True,
        history=history,
        split=SplitInfo(items=items),
    )


def _history(**kw: Any) -> History:
    rates = {"n1": 1.0, "n2": 1.0}
    return History(
        item_rate_by_node=rates,
        item_rate_samples_by_node=dict.fromkeys(rates, 20),
        env_hash_by_node=dict.fromkeys(rates, ENV),
        chunk_overhead_s=0.5,
        chunk_overhead_samples=40,
        **kw,
    )


def _samples() -> list[ChunkSample]:
    noise = [-1.2, 0.8, 0.3, -0.4, 1.1, 0.0, -0.7, 0.5]
    out = [ChunkSample(items=4, wall_s=4 * 1.5 + 12.0 + e) for e in noise]
    out += [
        ChunkSample(items=4, wall_s=4 * 1.5 + 12.0 + 9.0 + e, first=True)
        for e in (0.2, -0.3)
    ]
    out.append(ChunkSample(items=4, wall_s=900.0))  # a stalled chunk
    return out


# ---------------------------------------------------------------- determinism


def test_sample_order_never_changes_the_cost() -> None:
    base = node_chunk_cost(_samples(), 1.5)
    assert base is not None
    rng = random.Random(7)  # noqa: S311 - a test shuffle
    for _ in range(25):
        shuffled = _samples()
        rng.shuffle(shuffled)
        assert node_chunk_cost(shuffled, 1.5) == base


def test_chunk_costs_is_independent_of_mapping_insertion_order() -> None:
    a = {"b": _samples(), "a": _samples()}
    b = {"a": _samples(), "b": _samples()}
    rates = {"a": 1.5, "b": 1.5}
    assert list(chunk_costs(a, rates)) == list(chunk_costs(b, rates)) == ["a", "b"]
    assert chunk_costs(a, rates) == chunk_costs(b, rates)


def test_same_history_gives_a_byte_identical_decision_twice() -> None:
    history = _history(
        chunk_fixed_s_by_node={"n1": 0.2, "n2": 20.0},
        chunk_fixed_samples_by_node={"n1": 30, "n2": 12},
        chunk_first_s_by_node={"n2": 15.0},
        chunk_first_samples_by_node={"n2": 2},
    )
    request = _request(history, 3000)
    snap = _snap(_node("n1"), _node("n2"))
    one = candidates_json(plan(request, snap, "cost_model", now=NOW))
    two = candidates_json(plan(request, snap, "cost_model", now=NOW))
    assert one == two
    # node listing order is not an input either
    reordered = ClusterSnapshot(nodes=tuple(reversed(snap.nodes)), config=snap.config)
    three = candidates_json(plan(request, reordered, "cost_model", now=NOW))
    assert three == one


# ------------------------------------------------------------------- outliers


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -5.0, 1e12, -1e12])
def test_hostile_sample_values_are_ignored_not_propagated(bad: float) -> None:
    good = [ChunkSample(items=4, wall_s=4 * 1.5 + 12.0) for _ in range(5)]
    poisoned = [
        *good,
        ChunkSample(items=4, wall_s=bad),
        ChunkSample(items=4, wall_s=20.0, stage_s=bad),
        ChunkSample(items=4, wall_s=20.0, env_s=bad),
        ChunkSample(items=0, wall_s=20.0),
        ChunkSample(items=-3, wall_s=20.0),
    ]
    cost = node_chunk_cost(poisoned, 1.5)
    assert cost is not None
    assert cost.fixed_s == pytest.approx(12.0)
    assert 5 <= cost.fixed_samples <= 7


@pytest.mark.parametrize("rate", [0.0, -1.0, math.nan, math.inf, 1e12])
def test_an_unusable_item_rate_gives_no_cost(rate: float) -> None:
    assert node_chunk_cost(_samples(), rate) is None


def test_a_majority_of_stalled_chunks_is_the_only_way_to_move_the_median() -> None:
    steady = [ChunkSample(items=2, wall_s=2.0 + 5.0) for _ in range(7)]
    stalled = [ChunkSample(items=2, wall_s=600.0) for _ in range(3)]
    cost = node_chunk_cost(steady + stalled, 1.0)
    assert cost is not None and cost.fixed_s == pytest.approx(5.0)


# ------------------------------------------------------------------- fallback


def test_a_node_with_two_samples_uses_the_shared_overhead_in_the_plan() -> None:
    thin = _history(
        chunk_fixed_s_by_node={},  # the reader gave n2 nothing (too few rows)
        chunk_fixed_samples_by_node={},
    )
    request = _request(thin, 2000)
    decision = plan(request, _snap(_node("n1"), _node("n2")), "cost_model", now=NOW)
    for row in candidates_json(decision):
        terms = (row.get("estimate") or {}).get("terms", {})
        if "chunk_overhead" in terms:
            assert terms["chunk_overhead"]["detail"].get("per_node") is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -7.0])
def test_hostile_learned_costs_in_history_never_make_a_non_finite_plan(
    bad: float,
) -> None:
    """A corrupt or hand-edited replay must not poison the ranking with NaN."""
    history = _history(
        chunk_fixed_s_by_node={"n1": 0.2, "n2": bad},
        chunk_fixed_samples_by_node={"n1": 30, "n2": 12},
        chunk_first_s_by_node={"n2": bad},
        chunk_first_samples_by_node={"n2": 2},
    )
    decision = plan(
        _request(history, 1000), _snap(_node("n1"), _node("n2")), "cost_model", now=NOW
    )
    for row in candidates_json(decision):
        est = row.get("estimate")
        if est and est.get("total_s") is not None:
            assert math.isfinite(est["total_s"]), row
            assert est["total_s"] >= 0.0, row
