"""V1.0 G9-B tester: adversarial checks of the GPU path (D432, D436, Mode 3 GPU
lanes) at the planner/agent-selection level. Every probe is injected; this
machine's GPU and torch never decide a test."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

import pytest

from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
    TreeNeed,
)
from meshforge.controller.planner import (
    plan as _plan,
)
from meshforge.controller.planner.feasibility import static_rejection
from meshforge.controller.planner.types import RejectCode

pytestmark = pytest.mark.unit

GIB = 1024**3
MB = 1_000_000
PROJECT = "sha256:" + "a" * 64
VENV_GPU = frozenset({"exec", "exec-venv", "exec-gpu", "exec-split"})
DOCKER_GPU = frozenset({"exec", "exec-docker", "exec-gpu", "exec-split"})
NO_GPU = frozenset({"exec", "exec-venv", "exec-subprocess", "exec-split"})


def node(node_id: str, caps: frozenset[str], gpu: bool, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": caps,
        "ram_bytes": 16 * GIB,
        "cpu_threads": 8,
        "cpu_slots": 1,
        "gpu_slots": 1 if gpu else 0,
        "gpu_vram_bytes": (8 * GIB,) if gpu else (),
        "gpu_indices": (0,) if gpu else (),
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(items: int = 3000, **kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "required",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "trees": (TreeNeed(PROJECT, 10 * MB),),
        "submitter_node_id": "n_sub",
        "seeded": True,
        "history": History(item_rate_by_node={"n1": 50.0, "n2": 30.0, "n3": 500.0}),
        "split": SplitInfo(items=items),
    }
    base.update(kw)
    return PlanRequest(**base)


def snap(*nodes: NodeSnapshot) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=tuple(sorted(nodes, key=lambda n: n.node_id)))


NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)


def plan(req: PlanRequest, fabric: ClusterSnapshot) -> Any:
    return _plan(req, fabric, "cost_model", NOW)


def lanes_of(decision: Any) -> tuple[str, ...]:
    assert decision.split is not None
    return decision.split.node_ids


def test_venv_only_gpu_node_gets_gpu_job_but_no_gpu_node_never() -> None:
    gpu = node("n1", VENV_GPU, True)
    cpu = node("n3", NO_GPU, False)
    req = request(split=None, gpu="required")
    assert static_rejection(req, gpu) is None
    rej = static_rejection(req, cpu)
    assert rej is not None and rej.code is RejectCode.NO_GPU


def test_no_gpu_node_never_in_lanes_even_if_much_faster() -> None:
    fabric = snap(
        node("n1", VENV_GPU, True),
        node("n2", DOCKER_GPU, True),
        node("n3", NO_GPU, False),
    )
    decision = plan(request(), fabric)
    assert decision.chosen is not None
    assert "n3" not in lanes_of(decision)
    assert set(lanes_of(decision)) <= {"n1", "n2"}


def test_gpu_slots_exhausted_on_one_lane_node_excludes_it() -> None:
    fabric = snap(
        node("n1", VENV_GPU, True, used_gpu_slots=1, busy_gpus=frozenset({0})),
        node("n2", DOCKER_GPU, True),
        node("n3", NO_GPU, False),
    )
    decision = plan(request(), fabric)
    assert decision.chosen is not None
    assert lanes_of(decision) == ("n2",)  # one GPU node left: not a split


def test_every_gpu_slot_busy_means_no_gpu_lane_anywhere() -> None:
    fabric = snap(
        node("n1", VENV_GPU, True, used_gpu_slots=1, busy_gpus=frozenset({0})),
        node("n2", DOCKER_GPU, True, used_gpu_slots=1, busy_gpus=frozenset({0})),
        node("n3", NO_GPU, False),
    )
    decision = plan(request(), fabric)
    assert decision.chosen is None


def test_slot_counted_but_gpu_free_is_still_exhausted() -> None:
    # a GPU slot is free only while a GPU is (min of both)
    n = node("n1", VENV_GPU, True, gpu_slots=2, busy_gpus=frozenset({0}))
    assert n.free_gpu_slots == 0
    rej = static_rejection(request(split=None), n)
    # static check must not claim the node can never run it forever
    assert rej is None or rej.code is not RejectCode.NO_GPU


def test_gpu_node_offline_mid_split_replans_without_it() -> None:
    both = snap(node("n1", VENV_GPU, True), node("n2", DOCKER_GPU, True))
    first = plan(request(), both)
    assert first.chosen is not None
    lost = snap(
        node("n1", VENV_GPU, True, state="lost", connected=False),
        node("n2", DOCKER_GPU, True),
    )
    second = plan(request(attempt_no=2), lost)
    assert second.chosen is not None
    assert lanes_of(second) == ("n2",)
    for cand in second.candidates:
        if cand.node_id == "n1":
            assert not cand.feasible


def test_all_gpu_nodes_gone_never_falls_back_to_no_gpu_node() -> None:
    fabric = snap(
        node("n1", VENV_GPU, True, state="lost", connected=False),
        node("n3", NO_GPU, False),
    )
    decision = plan(request(), fabric)
    assert decision.chosen is None


def test_replan_remainder_keeps_gpu_requirement() -> None:
    fabric = snap(node("n1", VENV_GPU, True), node("n3", NO_GPU, False))
    req = request(items=7)
    req = replace(req, split=SplitInfo(items=7, min_nodes=1))
    decision = plan(req, fabric)
    assert decision.chosen is not None
    assert lanes_of(decision) == ("n1",)


@pytest.mark.parametrize("excl", [("n1",), ("n2",), ("n1", "n2")])
def test_exclude_nodes_applies_to_gpu_lanes(excl: tuple[str, ...]) -> None:
    fabric = snap(node("n1", VENV_GPU, True), node("n2", DOCKER_GPU, True))
    decision = plan(request(exclude_nodes=excl), fabric)
    if len(excl) == 2:
        assert decision.chosen is None
    else:
        assert decision.chosen is not None
        assert not set(lanes_of(decision)) & set(excl)
