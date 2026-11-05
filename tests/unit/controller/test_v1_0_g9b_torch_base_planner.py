"""V1.0 G9-B item 2 (D432): the planner places a ``cuda-pytorch`` job only
where PyTorch is promised - ``exec-docker``, or ``exec-subprocess`` +
``exec-torch`` - and rejects every other node ``no_runtime_for_base`` with a
text naming the fix. The venv GPU recipe (``python-cpu`` + ``gpu: required``)
is never touched by the rule, and the submit-time check (409) sees it too."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from meshforge.common.manifest import parse_manifest_data
from meshforge.controller.jobs.placement import PlacementError, check_auto
from meshforge.controller.planner import NodeSnapshot, PlanRequest, TreeNeed, plan
from meshforge.controller.planner.feasibility import static_rejection
from meshforge.controller.planner.types import ClusterSnapshot, RejectCode

pytestmark = pytest.mark.unit

GIB = 1024**3
G9B = "env-policy:" + "c" * 12


def node(node_id: str, caps: set[str], **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        # a G9-B agent always sends its env-policy tag (the D432 marker)
        "capabilities": frozenset({"exec", G9B, *caps}),
        "ram_bytes": 16 * GIB,
        "cpu_threads": 8,
        "cpu_slots": 1,
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
        "base": "cuda-pytorch",
        "env_key": "e" * 64,
        "trees": (TreeNeed("sha256:" + "a" * 64, 1024),),
    }
    base.update(kw)
    return PlanRequest(**base)


@pytest.mark.parametrize(
    ("caps", "allowed"),
    [
        ({"exec-docker"}, True),
        ({"exec-docker", "exec-subprocess"}, True),
        ({"exec-subprocess", "exec-torch"}, True),
        ({"exec-subprocess", "exec-torch", "exec-venv"}, True),
        ({"exec-subprocess"}, False),  # the Windows bundle without Docker
        ({"exec-subprocess", "exec-venv"}, False),
        ({"exec-venv"}, False),
        ({"exec-torch"}, False),  # a tag without the runtime it speaks of
        (set(), False),
    ],
)
def test_cuda_pytorch_needs_docker_or_torch_in_the_agent(
    caps: set[str], allowed: bool
) -> None:
    refused = static_rejection(request(), node("n1", caps))
    if allowed:
        assert refused is None
        return
    assert refused is not None
    assert refused.code is RejectCode.NO_RUNTIME_FOR_BASE
    if caps == {"exec-venv"}:
        # the V0.6 text, so recorded decisions replay unchanged
        assert refused.text == (
            "env.base cuda-pytorch needs Docker or the subprocess runtime; the "
            "node only offers the venv runtime"
        )
    else:
        assert "install Docker, or PyTorch for the agent" in refused.text
        assert "python-cpu" in refused.text and "requirements.txt" in refused.text


def test_python_cpu_and_the_venv_gpu_recipe_are_never_blocked() -> None:
    windows = node(
        "n1",
        {"exec-subprocess", "exec-venv", "exec-gpu"},
        gpu_vram_bytes=(4 * GIB,),
        gpu_slots=1,
    )
    assert static_rejection(request(base="python-cpu"), windows) is None
    assert static_rejection(request(base="python-cpu", gpu="required"), windows) is None


def test_the_planner_skips_the_torchless_node_for_the_docker_one() -> None:
    torchless = node("n_a", {"exec-subprocess"}, cpu_slots=4)
    docker = node("n_b", {"exec-docker"})
    decision = plan(
        request(), ClusterSnapshot(nodes=(torchless, docker)), "least_loaded"
    )
    assert decision.chosen is not None and decision.chosen.node_id == "n_b"
    row = next(c for c in decision.candidates if c.node_id == "n_a")
    assert row.rejection is not None
    assert row.rejection.code is RejectCode.NO_RUNTIME_FOR_BASE


class _Record:
    def __init__(self, node_id: str, caps: list[str]) -> None:
        from meshforge.controller.liveness import NodeState

        self.node_id = node_id
        self.state = NodeState.ONLINE
        self.hardware = {"capabilities": caps, "cpuThreads": 8}
        self.hardware_scores = None
        self.cpu_slots = 1
        self.gpu_slots = 0


def test_submit_refuses_cuda_pytorch_when_only_torchless_nodes_exist() -> None:
    manifest = parse_manifest_data(
        {"name": "t", "entrypoint": "python t.py", "env": {"base": "cuda-pytorch"}}
    )
    nodes: Any = [_Record("node_a", ["exec", "exec-subprocess", "exec-venv", G9B])]
    with pytest.raises(PlacementError) as info:
        check_auto(manifest, nodes)
    (rejection,) = info.value.details["rejections"]
    assert rejection["reject_code"] == "no_runtime_for_base"
    assert "install Docker" in rejection["reject_text"]
    ok: Any = [_Record("node_b", ["exec", "exec-subprocess", "exec-torch", G9B])]
    check_auto(manifest, ok)  # does not raise


@pytest.mark.parametrize(
    ("caps", "allowed"),
    [
        ({"exec-subprocess"}, True),  # V0.6: the subprocess runtime was enough
        ({"exec-subprocess", "exec-venv"}, True),
        ({"exec-docker"}, True),
        ({"exec-venv"}, False),  # the only V0.6 refusal
        (set(), True),  # no runtime tag at all: V0.6 did not refuse it here
    ],
)
def test_a_node_without_the_g9b_marker_keeps_the_v06_rule(
    caps: set[str], allowed: bool
) -> None:
    """Reviewer G9-B r1: an agent not upgraded yet (or a snapshot recorded
    before G9-B) has no ``env-policy:`` tag and is judged as before."""
    old = NodeSnapshot(
        node_id="n_old",
        state="online",
        connected=True,
        capabilities=frozenset({"exec", *caps}),
        ram_bytes=16 * GIB,
        cpu_threads=8,
        cpu_slots=1,
        gpu_slots=0,
    )
    refused = static_rejection(request(), old)
    assert (refused is None) is allowed
    if refused is not None:
        assert refused.text == (
            "env.base cuda-pytorch needs Docker or the subprocess runtime; the "
            "node only offers the venv runtime"
        )


def test_a_pre_g9b_cuda_pytorch_replay_on_a_subprocess_node_reproduces() -> None:
    """Reviewer G9-B r1 (BLOCKING 1): a decision recorded before G9-B -
    ``cuda-pytorch`` placed on an ``exec-subprocess`` node with no
    ``exec-torch`` (and no ``env-policy:`` tag: the agent predates both) - is
    replayed from its stored JSON (no ``exclude_nodes`` key either) and picks
    the same node with the same candidates, field for field."""
    import json

    from mf_planner_golden import decision_view

    from meshforge.controller.planner.replay import from_replay, to_replay

    subprocess_node = NodeSnapshot(
        node_id="n_sub",
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-subprocess", "exec-gpu"}),
        ram_bytes=16 * GIB,
        cpu_threads=8,
        cpu_slots=2,
        gpu_slots=0,
    )
    venv_node = dataclasses.replace(
        subprocess_node, node_id="n_venv", capabilities=frozenset({"exec", "exec-venv"})
    )
    snap = ClusterSnapshot(nodes=(subprocess_node, venv_node))
    recorded = plan(request(), snap, "least_loaded")
    assert recorded.chosen is not None and recorded.chosen.node_id == "n_sub"
    stored = json.loads(json.dumps(to_replay(request(), snap, None)))
    del stored["request"]["exclude_nodes"]  # the pre-G9-B request shape
    replayed = plan(*from_replay(stored)[:2], "least_loaded")
    assert decision_view(replayed) == decision_view(recorded)
    assert replayed.snapshot_digest == recorded.snapshot_digest
