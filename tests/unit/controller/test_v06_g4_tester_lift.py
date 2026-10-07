"""V0.6 G4 tester round 1 (D129): the retry-exclusion lift under a randomised
oracle and the mixed-tier cases.

The oracle uses only the feasibility rules (``static_rejection`` /
``rejection``) and the decision text of D129, never ``_with_exclusion``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
from typing import Any

import pytest

from meshforge.controller.planner import (
    ClusterSnapshot,
    NodeSnapshot,
    PastFailure,
    PlanRequest,
    RejectCode,
    TreeNeed,
    exclusions,
    plan,
)
from meshforge.controller.planner.feasibility import rejection, static_rejection
from meshforge.controller.planner.retry_exclusion import LIFT_ORDER, NEVER_LIFTED

GIB = 1024**3
EXEC = frozenset({"exec", "exec-subprocess"})
EXEC_GPU = frozenset({"exec", "exec-docker", "exec-gpu"})
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
TREE = "sha256:" + "a" * 64
TYPES = [
    "node_lost",
    "timeout",
    "transfer_error",
    "integrity_error",
    "disk_full",
    "vram_oom",
]


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job",
        "attempt_no": 3,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e",
        "trees": (TreeNeed(TREE, 1),),
    }
    base.update(kw)
    return PlanRequest(**base)


def gpu_node(node_id: str, vram_gb: int, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC_GPU,
        "cpu_slots": 1,
        "gpu_vram_bytes": (vram_gb * GIB,),
        "gpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def failed(node_id: str, kind: str, ago_s: float) -> PastFailure:
    return PastFailure(node_id, kind, NOW - dt.timedelta(seconds=ago_s), ())


def random_fabric(rng: random.Random) -> tuple[ClusterSnapshot, dict[str, str]]:
    nodes = []
    for i in range(rng.randint(1, 6)):
        online = rng.random() < 0.6
        nodes.append(
            NodeSnapshot(
                node_id=f"n{i}",
                state="online" if online else rng.choice(["offline", "suspect"]),
                connected=online or rng.random() < 0.2,
                capabilities=EXEC if rng.random() < 0.9 else frozenset(),
                cpu_slots=1,
                used_cpu_slots=rng.choice([0, 0, 1]),
            )
        )
    excluded = {n.node_id: rng.choice(TYPES) for n in nodes if rng.random() < 0.7}
    return ClusterSnapshot(nodes=tuple(nodes)), excluded


@pytest.mark.parametrize("seed", range(300))
def test_lift_oracle(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    snap, excluded = random_fabric(rng)
    req = request(excluded=excluded)
    decision = plan(req, snap, "cost_model")
    cap = snap.config.max_attempts_per_node
    by_id = {n.node_id: n for n in snap.nodes}
    elsewhere = [
        n
        for n in snap.nodes
        if n.node_id not in excluded and static_rejection(req, n) is None
    ]

    def usable(node_id: str) -> bool:
        lifted = dataclasses.replace(
            req, excluded={k: v for k, v in excluded.items() if k != node_id}
        )
        return (
            rejection(lifted, by_id[node_id], max_attempts_per_node=cap, local=False)
            is None
        )

    chosen = decision.chosen.node_id if decision.chosen else None
    if chosen in excluded:
        assert not elsewhere, "an exclusion was lifted while another node could run it"
        kind = excluded[chosen]
        assert kind not in NEVER_LIFTED, "disk_full lifted early"
        assert usable(chosen), "lifted onto a node that would not take the job now"
        tier = next(i for i, t in enumerate(LIFT_ORDER) if kind in t)
        for earlier in LIFT_ORDER[:tier]:
            for node_id, k in excluded.items():
                if k in earlier and static_rejection(req, by_id[node_id]) is None:
                    assert not usable(node_id), (
                        f"{chosen} ({kind}) lifted before usable {node_id} ({k})"
                    )
    if chosen is None and not elsewhere:
        for node_id, kind in excluded.items():
            if kind in NEVER_LIFTED or static_rejection(req, by_id[node_id]):
                continue
            assert not usable(node_id), f"{node_id} was usable but the job waited"
    if chosen is None and elsewhere:
        # a non-excluded node that would take the job now must have been chosen
        for node in elsewhere:
            assert rejection(req, node, max_attempts_per_node=cap, local=False), (
                f"{node.node_id} is free and not excluded but nothing was chosen"
            )
    # determinism (checklist 16): the same inputs, the same decision
    assert plan(req, snap, "cost_model") == decision


def test_a_pinned_job_is_never_excluded_by_its_own_history() -> None:
    snap = ClusterSnapshot(
        nodes=(
            NodeSnapshot("a", "online", True, EXEC, cpu_slots=1),
            NodeSnapshot("b", "online", True, EXEC, cpu_slots=1),
        )
    )
    req = request(excluded={"a": "node_lost", "b": "disk_full"}, pinned_node_id="a")
    chosen = plan(req, snap, "cost_model").chosen
    assert chosen is not None and chosen.node_id == "a"


def _two_reasons_scenario() -> None:
    """A GPU node lost (transient) *and* out of VRAM (the strongest reason) is
    a ``vram_oom`` node: D129 lifts ``vram_oom`` last, so the job must try the
    node that only had a transfer error first. ``exclusions`` keeps the first
    failure per node (attempts are read oldest first), so the transient one
    hides the VRAM one and the job goes back to the GPU that cannot hold it."""
    small, big = gpu_node("g-small", 8), gpu_node("g-big", 16)
    snap = ClusterSnapshot(nodes=(small, big))
    past = [  # oldest first, as queue_reads reads them
        failed("g-small", "node_lost", 100),
        failed("g-big", "transfer_error", 50),
        failed("g-small", "vram_oom", 10),
    ]
    ex = exclusions(past, snap.nodes, now=NOW, exclude_s=600.0)
    assert ex["g-small"] == "vram_oom", ex
    req = request(gpu="required", excluded=ex)
    chosen = plan(req, snap, "cost_model").chosen
    assert chosen is not None and chosen.node_id == "g-big"


@pytest.mark.xfail(
    strict=True,
    reason="T-6: exclusions() keeps the OLDEST failure type per node, so a "
    "node_lost followed by vram_oom is lifted in the node_lost tier (D129 "
    "says vram_oom last)",
)
def test_mixed_tier_label_is_the_strongest_reason_not_the_oldest() -> None:
    _two_reasons_scenario()


def test_a_waiting_job_has_a_stable_reason_key() -> None:
    """Nothing usable: the decision is the same on every tick (the dispatcher
    records a waiting decision only when ``reason_key`` changes - no growth, no
    busy loop)."""
    nodes = (
        NodeSnapshot("a", "offline", False, EXEC, cpu_slots=1),
        NodeSnapshot("b", "online", True, EXEC, cpu_slots=1, used_cpu_slots=1),
    )
    snap = ClusterSnapshot(nodes=nodes)
    req = request(excluded={"a": "node_lost", "b": "transfer_error"})
    first, second = plan(req, snap, "cost_model"), plan(req, snap, "cost_model")
    assert first.chosen is None and first == second
    assert first.reason_key == second.reason_key
    codes = {c.node_id: c.rejection.code for c in first.candidates if c.rejection}
    assert codes["a"] is RejectCode.NOT_ONLINE
    assert codes["b"] is RejectCode.RETRY_EXCLUDED
