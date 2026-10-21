"""V0.10 G8 tester (independent): forced strategies in the planner.

``mode3:<set>`` = split over exactly the set or wait; ``mode1:<node>`` for a
split family = pinned "don't split" on that node; a malformed stored set is
never trusted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.jobs.planning import MAX_FORCED_SET, forced_set
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
    TreeNeed,
    plan,
)

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess", "exec-split"})


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


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "trees": (TreeNeed("sha256:" + "a" * 64, 10_000_000),),
        "submitter_node_id": "n1",
        "seeded": True,
        "split": SplitInfo(items=3000),
        "history": History(item_rate_by_node={"n1": 50, "n2": 50, "n3": 50, "n4": 50}),
    }
    base.update(kw)
    return PlanRequest(**base)


def cluster(*nodes: NodeSnapshot) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=nodes)


def test_forced_set_of_four_uses_all_four_and_only_them() -> None:
    snap = cluster(node("n1"), node("n2"), node("n3"), node("n4"), node("n5"))
    d = plan(request(node_set=("n1", "n2", "n3", "n4")), snap, "cost_model")
    assert d.split is not None and d.split.split
    assert set(d.split.node_ids) == {"n1", "n2", "n3", "n4"}


@pytest.mark.parametrize(
    "broken",
    [
        {"connected": False},
        {"state": "draining"},
        {"capabilities": frozenset({"exec", "exec-subprocess"})},  # no exec-split
    ],
)
def test_forced_set_never_substitutes_a_node(broken: dict[str, Any]) -> None:
    snap = cluster(node("n1"), node("n2"), node("n3", **broken), node("n4"))
    d = plan(request(node_set=("n1", "n2", "n3")), snap, "cost_model")
    assert d.chosen is None, "a forced set with a dead member must wait, not shrink"
    assert d.split is None or not d.split.split


def test_forced_set_naming_an_unknown_node_waits() -> None:
    snap = cluster(node("n1"), node("n2"))
    d = plan(request(node_set=("n1", "n2", "ghost")), snap, "cost_model")
    assert d.chosen is None


def test_forced_set_does_not_depend_on_input_order() -> None:
    snap = cluster(node("n1"), node("n2"), node("n3"))
    a = plan(request(node_set=("n3", "n1", "n2")), snap, "cost_model")
    b = plan(request(node_set=("n1", "n2", "n3")), snap, "cost_model")
    assert a.split is not None and b.split is not None
    assert a.split.node_ids == b.split.node_ids


def test_pinned_mode1_for_a_split_job_does_not_split() -> None:
    snap = cluster(node("n1"), node("n2"), node("n3"))
    d = plan(request(pinned_node_id="n2"), snap, "cost_model")
    assert d.chosen is not None
    assert d.chosen.mode != "split"
    assert d.chosen.node_id == "n2"
    assert d.split is None or not d.split.split


def test_pinned_to_an_offline_node_does_not_fall_back() -> None:
    snap = cluster(node("n1"), node("n2", connected=False), node("n3"))
    d = plan(request(pinned_node_id="n2"), snap, "cost_model")
    assert d.chosen is None


def _job(raw: Any) -> Any:
    return SimpleNamespace(placement={"mode": "auto", "node_set": raw})


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "n1,n2",
        {"n1": 1},
        [1, 2],
        ["n1", ""],
        ["n1", None],
        [f"n{i}" for i in range(MAX_FORCED_SET + 1)],
    ],
)
def test_malformed_stored_node_set_is_never_trusted(raw: Any) -> None:
    assert forced_set(_job(raw)) == ()


def test_stored_node_set_is_sorted_and_deduplicated() -> None:
    assert forced_set(_job(["n3", "n1", "n3", "n2"])) == ("n1", "n2", "n3")
    assert forced_set(SimpleNamespace(placement={})) == ()
