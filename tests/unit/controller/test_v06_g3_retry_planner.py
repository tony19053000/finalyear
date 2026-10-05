"""V0.6 G3 (D110): which nodes a retry avoids, and how the planner uses it.

Pure: snapshots by hand, an injected ``now``. Covered: each failure type's
exclusion (node + time window, vram_oom by GPU size, none for the rest), the
``retry_excluded`` rejection, the soft fallback (no other node could ever run
it -> retry where it failed), pinned jobs never excluding, determinism.
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
from meshforge.controller.planner.feasibility import live_rejection

GIB = 1024**3
EXEC = frozenset({"exec", "exec-subprocess"})
EXEC_GPU = frozenset({"exec", "exec-docker", "exec-gpu"})
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)
TREE = "sha256:" + "a" * 64


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def gpu(node_id: str, *vram_gb: int, **kw: Any) -> NodeSnapshot:
    return node(
        node_id,
        capabilities=EXEC_GPU,
        gpu_vram_bytes=tuple(v * GIB for v in vram_gb),
        gpu_slots=len(vram_gb),
        **kw,
    )


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job",
        "attempt_no": 2,
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


def failed(
    node_id: str, kind: str, ago_s: float = 1.0, gpus: tuple[int, ...] = ()
) -> PastFailure:
    return PastFailure(node_id, kind, NOW - dt.timedelta(seconds=ago_s), gpus)


@pytest.mark.parametrize(
    "kind", ["node_lost", "transfer_error", "integrity_error", "timeout"]
)
def test_these_failures_exclude_their_node_for_the_window(kind: str) -> None:
    nodes = [node("a"), node("b")]
    assert exclusions([failed("a", kind, 599)], nodes, now=NOW, exclude_s=600) == {
        "a": kind
    }
    assert exclusions([failed("a", kind, 600)], nodes, now=NOW, exclude_s=600) == {}
    assert exclusions([failed("a", kind)], nodes, now=NOW, exclude_s=0) == {}


@pytest.mark.parametrize(
    "kind", ["resource_busy", "network_error", "output_error", "user_error"]
)
def test_other_failures_exclude_nothing(kind: str) -> None:
    assert exclusions([failed("a", kind)], [node("a")], now=NOW, exclude_s=600) == {}


def test_vram_oom_excludes_every_gpu_no_larger_than_the_one_that_ran_out() -> None:
    nodes = [gpu("a", 4, 8), gpu("b", 4), gpu("c", 8), gpu("d", 12), node("cpu")]
    # it ran on a's 8 GB GPU (index 1): 4 and 8 GB nodes are out, 12 GB is in,
    # a CPU-only node is not a GPU retry target the rule speaks about
    got = exclusions([failed("a", "vram_oom", gpus=(1,))], nodes, now=NOW, exclude_s=0)
    assert got == {"a": "vram_oom", "b": "vram_oom", "c": "vram_oom"}
    # on the 4 GB GPU: only nodes whose largest GPU is <= 4 GB (and a itself)
    got = exclusions([failed("a", "vram_oom", gpus=(0,))], nodes, now=NOW, exclude_s=0)
    assert got == {"a": "vram_oom", "b": "vram_oom"}
    # no time window for vram: a bigger GPU is the only thing that helps
    old = failed("b", "vram_oom", ago_s=10**6, gpus=(0,))
    assert exclusions([old], nodes, now=NOW, exclude_s=600) == {"b": "vram_oom"}


def test_a_retry_goes_elsewhere_and_says_why() -> None:
    snap = ClusterSnapshot(nodes=(node("a"), node("b")))
    excluded = exclusions(
        [failed("a", "node_lost")], snap.nodes, now=NOW, exclude_s=600
    )
    decision = plan(request(excluded=excluded), snap, "least_loaded")
    assert decision.chosen is not None and decision.chosen.node_id == "b"
    refused = {c.node_id: c.rejection for c in decision.candidates}["a"]
    assert refused is not None and refused.code is RejectCode.RETRY_EXCLUDED
    assert not refused.static and "node_lost" in refused.text
    # without the exclusion least_loaded's node-id tie-break picks a
    assert plan(request(), snap, "least_loaded").chosen.node_id == "a"  # type: ignore[union-attr]


def test_the_excluded_node_waits_while_another_capable_node_is_busy() -> None:
    snap = ClusterSnapshot(nodes=(node("a"), node("b", used_cpu_slots=1)))
    decision = plan(request(excluded={"a": "timeout"}), snap, "cost_model")
    assert decision.chosen is None and not decision.all_static_rejections


def test_exclusion_is_lifted_when_no_other_node_could_ever_run_the_job() -> None:
    lone = ClusterSnapshot(nodes=(node("a"),))
    decision = plan(request(excluded={"a": "node_lost"}), lone, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "a"
    # another node exists but can never run it (static): still lifted
    snap = ClusterSnapshot(nodes=(node("a"), node("b", capabilities=frozenset())))
    decision = plan(request(excluded={"a": "node_lost"}), snap, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "a"
    # vram_oom with no bigger GPU anywhere: retried on the same size
    gpus = ClusterSnapshot(nodes=(gpu("a", 4), gpu("b", 4)))
    excluded = exclusions(
        [failed("a", "vram_oom", gpus=(0,))], gpus.nodes, now=NOW, exclude_s=0
    )
    decision = plan(request(gpu="required", excluded=excluded), gpus, "cost_model")
    assert decision.chosen is not None


def test_a_pinned_job_never_excludes_its_node() -> None:
    snap = ClusterSnapshot(nodes=(node("a"), node("b")))
    pinned = request(pinned_node_id="a", excluded={"a": "node_lost"})
    decision = plan(pinned, snap, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "a"
    assert (
        live_rejection(pinned, node("a"), max_attempts_per_node=8, local=False) is None
    )


def test_exclusion_is_deterministic() -> None:
    snap = ClusterSnapshot(nodes=(node("a"), node("b"), node("c")))
    req = request(excluded={"b": "integrity_error"})
    first = plan(req, snap, "random")
    assert all(plan(req, snap, "random") == first for _ in range(5))
    assert first.chosen is not None and first.chosen.node_id != "b"
