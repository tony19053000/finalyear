"""V1.0 G9-B item 4: the planner side of ``exclude_nodes``.

* an excluded node is rejected ``excluded_by_user`` (a *static* code: it
  holds on every attempt, and a fabric of only excluded nodes is
  ``no_feasible_plan``), before any other reason;
* the list is read from the stored ``placement`` (sorted, unique, strings
  only, capped) - so retries and restarts keep it;
* a replay carries it and reproduces the decision; a replay without the key
  (stored before G9-B) decodes to "nobody excluded", and the decision digest
  of a request without exclusions is unchanged.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.controller.jobs.planning import MAX_EXCLUDE_NODES, excluded_nodes
from meshforge.controller.planner import NodeSnapshot, PlanRequest, TreeNeed, plan
from meshforge.controller.planner.feasibility import static_rejection
from meshforge.controller.planner.plan import snapshot_digest
from meshforge.controller.planner.replay import from_replay, to_replay
from meshforge.controller.planner.types import STATIC_CODES, ClusterSnapshot, RejectCode

pytestmark = pytest.mark.unit

GIB = 1024**3


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": frozenset({"exec", "exec-subprocess"}),
        "ram_bytes": 16 * GIB,
        "cpu_threads": 8,
        "cpu_slots": 2,
        "gpu_slots": 0,
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
        "trees": (TreeNeed("sha256:" + "a" * 64, 1024),),
    }
    base.update(kw)
    return PlanRequest(**base)


def test_the_code_is_static_and_comes_first() -> None:
    assert RejectCode.EXCLUDED_BY_USER in STATIC_CODES
    no_exec = node("n_a", capabilities=frozenset())
    refused = static_rejection(request(exclude_nodes=("n_a",)), no_exec)
    assert refused is not None and refused.code is RejectCode.EXCLUDED_BY_USER
    assert "--exclude-node" in refused.text
    assert static_rejection(request(exclude_nodes=("n_b",)), node("n_a")) is None


def test_the_planner_skips_the_excluded_node_and_fails_when_nothing_is_left() -> None:
    fast, other = node("n_a", cpu_slots=8), node("n_b")
    snap = ClusterSnapshot(nodes=(fast, other))
    decision = plan(request(exclude_nodes=("n_a",)), snap, "least_loaded")
    assert decision.chosen is not None and decision.chosen.node_id == "n_b"
    nothing = plan(request(exclude_nodes=("n_a", "n_b")), snap, "least_loaded")
    assert nothing.chosen is None and nothing.all_static_rejections


def _job(placement: dict[str, Any]) -> Any:
    return SimpleNamespace(placement=placement)


def test_the_stored_list_is_read_safely() -> None:
    assert excluded_nodes(_job({})) == ()
    assert excluded_nodes(_job({"exclude_nodes": "node_x"})) == ()
    assert excluded_nodes(_job({"exclude_nodes": ["b", "a", "b", 3, "", None]})) == (
        "a",
        "b",
    )
    many = [f"node_{i:02d}" for i in range(40)]
    assert len(excluded_nodes(_job({"exclude_nodes": many}))) == MAX_EXCLUDE_NODES


def test_replay_round_trip_and_old_replays() -> None:
    req = request(exclude_nodes=("n_a",))
    snap = ClusterSnapshot(nodes=(node("n_a"), node("n_b")))
    replay = json.loads(json.dumps(to_replay(req, snap, None)))
    assert replay["request"]["exclude_nodes"] == ["n_a"]
    again, snap2, now = from_replay(replay)
    assert again.exclude_nodes == ("n_a",)
    assert plan(again, snap2, "least_loaded", now).chosen.node_id == "n_b"  # type: ignore[union-attr]
    del replay["request"]["exclude_nodes"]  # stored before G9-B
    old, _, _ = from_replay(replay)
    assert old.exclude_nodes == ()


def test_no_exclusion_keeps_the_old_digest() -> None:
    snap = ClusterSnapshot(nodes=(node("n_a"),))
    plain = request()
    assert snapshot_digest(plain, snap) == snapshot_digest(
        dataclasses.replace(plain, exclude_nodes=()), snap
    )
    assert snapshot_digest(plain, snap) != snapshot_digest(
        dataclasses.replace(plain, exclude_nodes=("n_a",)), snap
    )
