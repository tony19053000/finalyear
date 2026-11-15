"""V1.0 G12 (D459): a new workload's lanes use the node's chunk costs learned
from any workload on the same runtime kind ("node history").

Covered: the pooled estimator (each workload's own rate, too few samples, a
workload without a rate); the runtime-kind prediction; ``with_node_history``
(runtime isolation, a node with the fingerprint's own cost is left alone, no
change without a match); the fallback order fingerprint -> node_history ->
shared -> assumed and its explain labels; the decision JSON; the replay round
trip and an older replay's digest; and the lab v0.17.3 case (24-item split,
L1 Docker + L2 venv: real 52 s, the shared-overhead planner said 38 s).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pytest

from meshforge.common.explain_text import term_note
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TermSource,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.chunk_cost import (
    MIN_FIXED_SAMPLES,
    ChunkSample,
    NodeChunkCost,
    PooledSample,
    expected_runtime,
    pooled_chunk_cost,
    with_node_history,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.plan import snapshot_digest
from meshforge.controller.planner.replay import from_replay, to_replay
from meshforge.controller.planner.split import (
    estimate_split,
    lane,
    set_chunk,
)
from meshforge.controller.planner.types import PlannerConfig

pytestmark = pytest.mark.unit

DOCKER = frozenset({"exec", "exec-docker", "exec-subprocess", "exec-split"})
VENV = frozenset({"exec", "exec-venv", "exec-subprocess", "exec-split"})
SUBPROCESS = frozenset({"exec", "exec-subprocess", "exec-split"})
PROJECT = "sha256:" + "a" * 64
ENV = "e" * 64
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.UTC)


def _node(node_id: str, caps: frozenset[str] = VENV, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": caps,
        "cpu_threads": 8,
        "cpu_slots": 1,
        "cached": frozenset({PROJECT}),
        "cached_envs": {ENV: 1},
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def _snap(*nodes: NodeSnapshot, split: SplitConfig | None = None) -> ClusterSnapshot:
    return ClusterSnapshot(
        nodes=tuple(sorted(nodes, key=lambda n: n.node_id)),
        config=PlannerConfig(split=split or SplitConfig()),
    )


def _history(rates: dict[str, float], **kw: Any) -> History:
    return History(
        item_rate_by_node=rates,
        item_rate_samples_by_node=dict.fromkeys(rates, 20),
        env_hash_by_node=dict.fromkeys(rates, ENV),
        **kw,
    )


def _request(items: int, history: History, **split_kw: Any) -> PlanRequest:
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
        split=SplitInfo(items=items, **split_kw),
    )


def _pooled(
    workload: str, n: int, *, items: int, spi: float, fixed: float, first: float = 0.0
) -> list[PooledSample]:
    """``n`` steady chunks of ``workload`` (+ one first chunk when ``first``)."""
    run = items * spi
    rows = [
        PooledSample(workload, ChunkSample(items, run + fixed), items / run)
        for _ in range(n)
    ]
    if first:
        rows.append(
            PooledSample(
                workload,
                ChunkSample(items, run + fixed + first, first=True),
                items / run,
            )
        )
    return rows


# ------------------------------------------------------------------ estimator


def test_pooled_cost_uses_each_workloads_own_rate() -> None:
    """Two workloads, very different item rates, the same node cost: a single
    rate would read the slow one's items as fixed cost."""
    rows = _pooled("fast", 4, items=4, spi=0.5, fixed=3.0, first=10.0)
    rows += _pooled("slow", 4, items=2, spi=9.0, fixed=3.0, first=10.0)
    cost = pooled_chunk_cost(rows)
    assert cost is not None
    assert cost.fixed_s == pytest.approx(3.0)
    assert cost.fixed_samples == 8
    assert cost.first_s == pytest.approx(10.0)
    assert cost.first_samples == 2


def test_pooled_cost_needs_the_same_minimum_samples() -> None:
    rows = _pooled("a", MIN_FIXED_SAMPLES - 1, items=4, spi=1.0, fixed=3.0)
    assert pooled_chunk_cost(rows) is None
    rows += _pooled("b", 1, items=4, spi=2.0, fixed=3.0)
    cost = pooled_chunk_cost(rows)
    assert cost is not None and cost.fixed_samples == MIN_FIXED_SAMPLES


def test_a_workload_without_a_usable_rate_contributes_nothing() -> None:
    rows = _pooled("ok", 3, items=4, spi=1.0, fixed=2.0)
    rows.append(PooledSample("broken", ChunkSample(4, 99.0), float("nan")))
    rows.append(PooledSample("zero", ChunkSample(4, 99.0), 0.0))
    cost = pooled_chunk_cost(rows)
    assert cost is not None
    assert cost.fixed_samples == 3 and cost.fixed_s == pytest.approx(2.0)
    assert pooled_chunk_cost([]) is None


# ------------------------------------------------------------------ runtime kind


@pytest.mark.parametrize(
    ("caps", "base", "expected"),
    [
        (DOCKER, "python-cpu", "docker"),
        (DOCKER, "cuda-pytorch", "docker"),
        (VENV, "python-cpu", "venv"),
        (VENV, "cuda-pytorch", None),  # no torch in the agent's interpreter
        (VENV | {"exec-torch"}, "cuda-pytorch", "subprocess"),
        (SUBPROCESS, "python-cpu", "subprocess"),
        (frozenset({"exec-venv"}), "cuda-pytorch", None),
        (frozenset(), "python-cpu", None),
    ],
)
def test_expected_runtime_follows_the_agents_preference(
    caps: frozenset[str], base: str, expected: str | None
) -> None:
    assert expected_runtime(caps, base) == expected


def test_with_node_history_matches_the_runtime_kind_only() -> None:
    costs = {
        ("L1", "docker"): NodeChunkCost(0.2, 30, 0.0, 2),
        ("L2", "venv"): NodeChunkCost(2.8, 13, 12.1, 2),
        ("L2", "subprocess"): NodeChunkCost(40.0, 9),  # never this job's kind
        ("L3", "docker"): NodeChunkCost(1.0, 5),  # L3 offers no Docker
    }
    nodes = [_node("L1", DOCKER), _node("L2", VENV), _node("L3", VENV)]
    got = with_node_history(History(), costs, nodes, "python-cpu")
    assert got.node_chunk_fixed_s_by_node == {"L1": 0.2, "L2": 2.8}
    assert got.node_chunk_fixed_samples_by_node == {"L1": 30, "L2": 13}
    assert got.node_chunk_first_s_by_node == {"L1": 0.0, "L2": 12.1}
    assert got.node_chunk_runtime_by_node == {"L1": "docker", "L2": "venv"}


def test_with_node_history_leaves_a_node_with_its_own_cost_alone() -> None:
    costs = {
        ("L1", "venv"): NodeChunkCost(5.0, 10),
        ("L2", "venv"): NodeChunkCost(6, 10),
    }
    own = History(
        chunk_fixed_s_by_node={"L1": 1.0}, chunk_fixed_samples_by_node={"L1": 4}
    )
    got = with_node_history(own, costs, [_node("L1"), _node("L2")], "python-cpu")
    assert got.node_chunk_fixed_s_by_node == {"L2": 6}
    assert got.chunk_fixed_s_by_node == {"L1": 1.0}
    # nothing matches -> the very same object (no empty keys anywhere)
    assert with_node_history(own, {}, [_node("L2")], "python-cpu") is own


# ------------------------------------------------------------------ fallback order


def _chunk_term(history: History, node_id: str = "n2") -> Any:
    snapshot = _snap(_node("n1"), _node("n2"))
    request = _request(400, history)
    item = lane(request, snapshot.node(node_id), "cpu", snapshot, local=False)
    return item.fixed, item.first


def test_fallback_order_fingerprint_then_node_history_then_shared_then_assumed() -> (
    None
):
    rates = {"n1": 1.0, "n2": 1.0}
    node_history = {
        "node_chunk_fixed_s_by_node": {"n2": 3.0},
        "node_chunk_fixed_samples_by_node": {"n2": 11},
        "node_chunk_first_s_by_node": {"n2": 12.0},
        "node_chunk_first_samples_by_node": {"n2": 2},
        "node_chunk_runtime_by_node": {"n2": "venv"},
    }
    own = _history(
        rates,
        chunk_fixed_s_by_node={"n2": 20.0},
        chunk_fixed_samples_by_node={"n2": 5},
        chunk_overhead_s=0.5,
        chunk_overhead_samples=9,
        **node_history,
    )
    fixed, first = _chunk_term(own)
    assert (fixed.seconds, fixed.detail["from"]) == (20.0, "node")
    assert first is None  # the fingerprint learned no first chunk: none
    pooled = dataclasses.replace(
        own, chunk_fixed_s_by_node={}, chunk_fixed_samples_by_node={}
    )
    fixed, first = _chunk_term(pooled)
    assert fixed.detail == {"from": "node_history", "samples": 11, "runtime": "venv"}
    assert fixed.seconds == 3.0 and fixed.source == TermSource.HISTORY
    assert first is not None and first.seconds == 12.0
    shared = dataclasses.replace(pooled, node_chunk_fixed_s_by_node={})
    fixed, first = _chunk_term(shared)
    assert (fixed.seconds, fixed.source) == (0.5, TermSource.HISTORY)
    assert first is None
    assumed = dataclasses.replace(shared, chunk_overhead_s=None)
    fixed, _ = _chunk_term(assumed)
    assert fixed.source == TermSource.ASSUMED
    assert fixed.seconds == SplitConfig().chunk_overhead_s


def test_decision_json_and_explain_name_the_source_per_node() -> None:
    snapshot = _snap(_node("n1"), _node("n2"), _node("n3"), _node("n4"))
    history = _history(
        dict.fromkeys(("n1", "n2", "n3", "n4"), 1.0),
        chunk_fixed_s_by_node={"n1": 0.2},
        chunk_fixed_samples_by_node={"n1": 30},
        node_chunk_fixed_s_by_node={"n2": 4.0},
        node_chunk_fixed_samples_by_node={"n2": 12},
        node_chunk_first_s_by_node={"n2": 9.0},
        node_chunk_first_samples_by_node={"n2": 3},
        node_chunk_runtime_by_node={"n2": "venv"},
    )
    decision = plan(_request(4000, history), snapshot, "cost_model", now=NOW)
    rows = [r for r in candidates_json(decision) if r["mode"] == "split"]
    term = next(
        r["estimate"]["terms"]["chunk_overhead"]
        for r in rows
        if r.get("estimate") and len(r["node_set"]) == 4
    )
    per_node = term["detail"]["per_node"]
    assert per_node["n1"]["fixed_from"] == "node"
    assert per_node["n2"] == {
        "fixed_s": 4.0,
        "fixed_source": "history",
        "fixed_from": "node_history",
        "fixed_samples": 12,
        "runtime": "venv",
        "first_s": 9.0,
        "first_samples": 3,
    }
    assert per_node["n3"]["fixed_from"] == "shared"
    assert per_node["n3"]["fixed_source"] == "assumed"
    note = term_note(term, name="chunk_overhead")
    assert note is not None
    assert "n1 0.2s (history n=30)" in note
    assert "n2 4.0s (node_history venv n=12) +9.0s first (n=3)" in note
    assert "n3 1.5s (assumed)" in note


def test_explain_label_shared_when_the_shared_overhead_is_measured() -> None:
    detail = {
        "per_node": {
            "a": {"fixed_s": 0.5, "fixed_source": "history", "fixed_from": "shared"},
            "b": {"fixed_s": 1.0, "fixed_source": "assumed", "fixed_from": "shared"},
            "c": {
                "fixed_s": 2.0,
                "fixed_source": "history",
                "fixed_from": "node_history",
                "fixed_samples": 7,
                "runtime": "<script>",
            },
        }
    }
    note = term_note({"detail": detail}, name="chunk_overhead")
    assert note == (
        "per chunk a 0.5s (shared), b 1.0s (assumed), c 2.0s (node_history n=7)"
    )


def test_no_node_history_keeps_every_decision_unchanged() -> None:
    snapshot = _snap(_node("n1"), _node("n2"))
    history = _history({"n1": 1.0, "n2": 0.5}, chunk_overhead_s=0.4)
    request = _request(300, history)
    again = dataclasses.replace(
        request,
        history=with_node_history(history, {}, snapshot.nodes, "python-cpu"),
    )
    first = plan(request, snapshot, "cost_model", now=NOW)
    second = plan(again, snapshot, "cost_model", now=NOW)
    assert candidates_json(first) == candidates_json(second)
    assert first.snapshot_digest == second.snapshot_digest


# ------------------------------------------------------------------ replay


def test_replay_round_trip_keeps_node_history_and_the_decision() -> None:
    snapshot = _snap(_node("n1"), _node("n2"))
    history = _history(
        {"n1": 1.0, "n2": 1.0},
        node_chunk_fixed_s_by_node={"n2": 6.0},
        node_chunk_fixed_samples_by_node={"n2": 8},
        node_chunk_first_s_by_node={"n2": 11.0},
        node_chunk_first_samples_by_node={"n2": 1},
        node_chunk_runtime_by_node={"n2": "venv"},
    )
    request = _request(500, history)
    replay = to_replay(request, snapshot, NOW)
    assert replay["request"]["history"]["node_chunk_runtime_by_node"] == {"n2": "venv"}
    back, snap, now = from_replay(replay)
    assert back.history == request.history
    assert candidates_json(plan(back, snap, "cost_model", now=now)) == candidates_json(
        plan(request, snapshot, "cost_model", now=NOW)
    )


def test_an_older_replay_without_the_keys_decodes_and_keeps_its_digest() -> None:
    snapshot = _snap(_node("n1"), _node("n2"))
    request = _request(500, _history({"n1": 1.0, "n2": 1.0}))
    replay = to_replay(request, snapshot, NOW)
    for key in list(replay["request"]["history"]):
        assert not key.startswith("node_chunk_"), key
    back, snap, _ = from_replay(replay)
    assert back.history.node_chunk_fixed_s_by_node == {}
    assert snapshot_digest(back, snap) == snapshot_digest(request, snapshot)


# ------------------------------------------------------------------ lab case


def _lab_costs() -> dict[tuple[str, str], NodeChunkCost]:
    """Node history from lab v0.17.3's big 480-item split (job_01M77D2FNRE7
    SCB1KSGDZ0NMS4, another workload): L1 Docker 6.1 s per 4-item chunk
    (fixed 0.21 s), L2 venv 26.2 s per chunk (fixed 2.79 s, +12.1 s on a
    lane's first chunk). L2's venv-rebuild first chunk (201 s, env build) is
    excluded by the D456 build rule. A decoy: L2's subprocess chunks of a
    third workload with a 30 s fixed cost - another runtime kind, unused."""
    l1 = _pooled("big", 12, items=4, spi=1.47, fixed=0.21, first=0.0001)
    l2 = _pooled("big", 12, items=4, spi=5.85, fixed=2.79, first=12.1)
    l2.append(
        PooledSample(
            "big",
            ChunkSample(4, 201.0, env_s=170.0, first=True),
            4 / 23.4,
        )
    )
    decoy = _pooled("other", 6, items=1, spi=3.0, fixed=30.0)
    out: dict[tuple[str, str], NodeChunkCost] = {}
    for key, rows in (
        (("L1", "docker"), l1),
        (("L2", "venv"), l2),
        (("L2", "subprocess"), decoy),
    ):
        cost = pooled_chunk_cost(rows)
        assert cost is not None
        out[key] = cost
    return out


def _lab_set_s(*, node_history: bool) -> float:
    """run3 job_01M77DM46NTQ3777EN6EDY18SP: 24 items, 2 per chunk, L1 (the
    submitter, Docker) + L2 (Windows venv); the workload's own chunks gave L2
    no per-node cost (shared 0.015 s). Item rates as the fingerprint's
    history read them (L1 2.3 s/item, L2 3.75 s/item from the venv chunk
    process)."""
    snapshot = _snap(_node("L1", DOCKER), _node("L2", VENV))
    history = _history(
        {"L1": 1 / 2.3, "L2": 1 / 3.75},
        chunk_overhead_s=0.015,
        chunk_overhead_samples=24,
    )
    if node_history:
        history = with_node_history(history, _lab_costs(), snapshot.nodes, "python-cpu")
        assert history.node_chunk_runtime_by_node == {"L1": "docker", "L2": "venv"}
        assert history.node_chunk_fixed_s_by_node["L2"] == pytest.approx(2.79)
        assert history.node_chunk_first_s_by_node["L2"] == pytest.approx(12.1)
    request = _request(24, history, chunk_items=2)
    lanes = [
        lane(request, snapshot.node(n), "cpu", snapshot, local=n == "L1")
        for n in ("L1", "L2")
    ]
    chunk = set_chunk(request, lanes, snapshot)
    assert chunk.items == 2 and chunk.chunks == 12
    return estimate_split(request, lanes, snapshot, chunk).total_s


def test_lab_case_node_history_predicts_within_20_percent_of_52_s() -> None:
    real = 52.0
    shared = _lab_set_s(node_history=False)
    learned = _lab_set_s(node_history=True)
    # the shared overhead under-predicts as in the lab (38.3 s)
    assert shared < 0.8 * real, shared
    assert abs(learned - real) / real <= 0.20, learned
    assert abs(learned - real) < abs(shared - real)
