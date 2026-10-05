"""V0.6 G4 (D129): how the planner lifts retry exclusions when every capable
node is excluded.

Pure: snapshots by hand, an injected ``now``. The G3 rule lifted *every*
exclusion as soon as no node outside the excluded set could run the job, and
judged "could run" statically only - so chaos seed 22 (three nodes lost and
offline, the fourth just ran out of disk) put every retry back on the full
node and burned the job's attempts there. D129: lift one failure-type tier at
a time (``node_lost``/``timeout`` first, ``vram_oom`` last), only onto nodes
that would take the job right now, never ``disk_full`` early; nothing usable
-> the job waits.
"""

from __future__ import annotations

import datetime as dt
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
from meshforge.controller.planner.retry_exclusion import (
    EXCLUDE_NODE_FOR,
    LIFT_ORDER,
    NEVER_LIFTED,
)

GIB = 1024**3
EXEC = frozenset({"exec", "exec-subprocess"})
EXEC_GPU = frozenset({"exec", "exec-docker", "exec-gpu"})
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
TREE = "sha256:" + "a" * 64
WINDOW = 600.0


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def offline(node_id: str) -> NodeSnapshot:
    return node(node_id, state="offline", connected=False)


def gpu(node_id: str, vram_gb: int, **kw: Any) -> NodeSnapshot:
    return node(
        node_id,
        capabilities=EXEC_GPU,
        gpu_vram_bytes=(vram_gb * GIB,),
        gpu_slots=1,
        **kw,
    )


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


def failed(node_id: str, kind: str, ago_s: float = 5.0) -> PastFailure:
    return PastFailure(node_id, kind, NOW - dt.timedelta(seconds=ago_s), ())


def reasons(decision: Any) -> dict[str, str]:
    return {
        c.node_id: (c.rejection.code.value if c.rejection else "ok")
        for c in decision.candidates
    }


SEED_22_PAST = [
    failed("a", "node_lost", 7),
    failed("b", "node_lost", 4),
    failed("c", "node_lost", 1),
    failed("d", "disk_full", 0.5),
]


def test_the_tiers_cover_every_excluding_type_once() -> None:
    tiered = [kind for tier in LIFT_ORDER for kind in tier]
    assert len(tiered) == len(set(tiered))
    assert set(tiered) | NEVER_LIFTED == EXCLUDE_NODE_FOR | {"vram_oom"}
    assert LIFT_ORDER[0] == {"node_lost", "timeout"}
    assert LIFT_ORDER[-1] == {"vram_oom"}


def test_chaos_seed_22_does_not_burn_attempts_on_the_full_node() -> None:
    """Three nodes lost and offline, the fourth full: the job waits."""
    snap = ClusterSnapshot(nodes=(offline("a"), offline("b"), offline("c"), node("d")))
    ex = exclusions(SEED_22_PAST, snap.nodes, now=NOW, exclude_s=WINDOW)
    assert ex == {
        "a": "node_lost",
        "b": "node_lost",
        "c": "node_lost",
        "d": "disk_full",
    }
    decision = plan(request(excluded=ex), snap, "cost_model")
    assert decision.chosen is None
    assert not decision.all_static_rejections  # waits, never no_feasible_plan
    assert reasons(decision)["d"] == RejectCode.RETRY_EXCLUDED.value
    # every policy agrees: the full node is never chosen
    for policy in ("random", "least_loaded", "round_robin", "cache_affinity"):
        assert plan(request(excluded=ex), snap, policy).chosen is None


def test_seed_22_a_lost_node_coming_back_takes_the_retry() -> None:
    snap = ClusterSnapshot(nodes=(offline("a"), node("b"), offline("c"), node("d")))
    ex = exclusions(SEED_22_PAST, snap.nodes, now=NOW, exclude_s=WINDOW)
    decision = plan(request(excluded=ex), snap, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "b"
    assert reasons(decision)["d"] == RejectCode.RETRY_EXCLUDED.value


def test_seed_22_the_full_node_is_tried_again_once_its_window_ends() -> None:
    snap = ClusterSnapshot(nodes=(offline("a"), offline("b"), offline("c"), node("d")))
    later = NOW + dt.timedelta(seconds=WINDOW)
    ex = exclusions(SEED_22_PAST, snap.nodes, now=later, exclude_s=WINDOW)
    assert ex == {}
    decision = plan(request(excluded=ex), snap, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "d"


def test_disk_full_alone_on_a_one_node_fabric_waits_for_the_window() -> None:
    lone = ClusterSnapshot(nodes=(node("a"),))
    decision = plan(request(excluded={"a": "disk_full"}), lone, "cost_model")
    assert decision.chosen is None and not decision.all_static_rejections


def test_transient_tier_is_lifted_before_vram_oom() -> None:
    snap = ClusterSnapshot(nodes=(gpu("a", 4), gpu("b", 4)))
    req = request(gpu="required", excluded={"a": "vram_oom", "b": "timeout"})
    decision = plan(req, snap, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "b"
    assert reasons(decision)["a"] == RejectCode.RETRY_EXCLUDED.value


def test_a_later_tier_is_lifted_when_the_earlier_one_has_no_usable_node() -> None:
    snap = ClusterSnapshot(nodes=(offline("a"), node("b")))
    req = request(excluded={"a": "node_lost", "b": "integrity_error"})
    decision = plan(req, snap, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "b"


def test_vram_oom_is_lifted_last_when_nothing_else_is_usable() -> None:
    snap = ClusterSnapshot(nodes=(gpu("a", 4), gpu("b", 4, state="suspect")))
    req = request(gpu="required", excluded={"a": "vram_oom", "b": "node_lost"})
    decision = plan(req, snap, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "a"


def test_never_lifted_onto_a_node_without_a_free_slot() -> None:
    snap = ClusterSnapshot(nodes=(node("a", used_cpu_slots=1), offline("b")))
    req = request(excluded={"a": "timeout", "b": "node_lost"})
    decision = plan(req, snap, "cost_model")
    assert decision.chosen is None and not decision.all_static_rejections
    # still excluded (not "slots_full"): the lift did not happen
    assert reasons(decision)["a"] == RejectCode.RETRY_EXCLUDED.value


def test_only_the_usable_nodes_of_a_tier_are_lifted() -> None:
    snap = ClusterSnapshot(nodes=(node("a"), offline("b"), node("c", used_cpu_slots=1)))
    req = request(excluded={"a": "node_lost", "b": "node_lost", "c": "timeout"})
    decision = plan(req, snap, "least_loaded")
    assert decision.chosen is not None and decision.chosen.node_id == "a"
    assert reasons(decision)["c"] == RejectCode.RETRY_EXCLUDED.value


def test_a_capable_node_outside_the_excluded_set_keeps_every_exclusion() -> None:
    # c is offline but could run the job some day: the job waits for it
    snap = ClusterSnapshot(nodes=(node("a"), offline("c")))
    decision = plan(request(excluded={"a": "node_lost"}), snap, "cost_model")
    assert decision.chosen is None
    assert reasons(decision)["a"] == RejectCode.RETRY_EXCLUDED.value


@pytest.mark.parametrize("policy", ["cost_model", "random", "least_loaded"])
def test_the_lift_is_deterministic(policy: str) -> None:
    snap = ClusterSnapshot(
        nodes=(node("a"), node("b"), offline("c"), gpu("d", 8), node("e"))
    )
    req = request(
        excluded={
            "a": "transfer_error",
            "b": "timeout",
            "c": "node_lost",
            "d": "vram_oom",
            "e": "disk_full",
        }
    )
    first = plan(req, snap, policy)
    assert first.chosen is not None and first.chosen.node_id == "b"
    assert all(plan(req, snap, policy) == first for _ in range(5))
