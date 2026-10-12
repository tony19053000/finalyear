"""V0.7 G5 part 2 (D145b, THREAT_MODEL CG-T2 / C4): hardware scores are bound to
the node whose certificate-authenticated session reported them.

* the stored document names that node (Hello and report paths - the node id
  the session proved, never a value inside the score set);
* readers (the planner's node snapshot, ``GET /v1/nodes``) never use a
  document bound to another node; a pre-G5 document without a binding is
  accepted until the next Hello re-binds it.
"""

from __future__ import annotations

import pytest
from mf_api_records import node_record
from mf_bench_fakes import make_scoreset
from test_session_hardware_scores import ScoresRegistry, _report, gpu_hello
from test_session_manager import FakeClock, admit, make_manager, settle

from meshforge.agent.benchmarks.wire import to_proto
from meshforge.common.ids import new_node_id
from meshforge.controller.api.shape import node_scores
from meshforge.controller.planner.inputs import bound_to, node_snapshot
from meshforge.controller.scores import scores_document

pytestmark = pytest.mark.unit

HW = {"cpu_threads": 8, "capabilities": ["exec", "hardware-scores"]}


def _doc(node_id: str) -> dict[str, object]:
    document, _ = scores_document(to_proto(make_scoreset()), node_id=node_id)
    return document


def test_the_document_names_the_reporting_node() -> None:
    node = str(new_node_id())
    document = _doc(node)
    assert document["node_id"] == node
    assert document["fingerprint"] == make_scoreset().fingerprint


def test_bound_to() -> None:
    node, other = str(new_node_id()), str(new_node_id())
    assert bound_to(_doc(node), node) is True
    assert bound_to(_doc(other), node) is False
    legacy = _doc(node)
    del legacy["node_id"]
    assert bound_to(legacy, node) is True  # pre-G5 row: until the next Hello
    assert bound_to(None, node) is False


def _planner_doc(node_id: str) -> dict[str, object]:
    document = _doc(node_id)
    metrics = document["metrics"]
    assert isinstance(metrics, list)
    metrics[0]["name"] = "cpu_fp32_gflops_all"  # the metric the planner reads
    return document


def test_the_planner_never_uses_another_nodes_scores() -> None:
    node, other = str(new_node_id()), str(new_node_id())
    own = node_snapshot(
        node,
        state="online",
        connected=True,
        hardware=HW,
        hardware_scores=_planner_doc(node),
    )
    assert own.cpu_score is not None
    foreign = node_snapshot(
        node,
        state="online",
        connected=True,
        hardware=HW,
        hardware_scores=_planner_doc(other),
    )
    assert foreign.cpu_score is None and foreign.gpu_score is None


def test_the_api_never_shows_another_nodes_scores() -> None:
    node, other = str(new_node_id()), str(new_node_id())
    hardware = {"capabilities": ["hardware-scores"]}
    own = node_record(node_id=node, hardware=hardware, hardware_scores=_doc(node))
    assert node_scores(own).status == "measured"
    foreign = node_record(node_id=node, hardware=hardware, hardware_scores=_doc(other))
    assert node_scores(foreign).status == "pending"


@pytest.mark.asyncio
async def test_hello_and_report_scores_are_bound_to_the_session_node() -> None:
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    h = gpu_hello(new_node_id(), 0)
    h.hardware_scores.CopyFrom(to_proto(make_scoreset()))
    live = await admit(manager, h)
    stored = registry.nodes[live.node_id].hardware_scores
    assert stored is not None and stored["node_id"] == live.node_id
    await manager.handle_message(live, _report(live, "s-1", to_proto(make_scoreset())))
    await settle()
    ((args, _kwargs),) = registry.calls_named("set_hardware_scores")
    assert args[0] == live.node_id and args[1]["node_id"] == live.node_id
