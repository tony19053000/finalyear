"""V0.7 G5 (D142): the controller's intake of advertised owner policies, and the
planner/submit checks that use them. No database."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common import owner_policy as op
from meshforge.common.manifest import Manifest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.jobs.placement import (
    PlacementError,
    check_auto,
    check_placement,
)
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import NodeSnapshot, PlanRequest, RejectCode
from meshforge.controller.planner.feasibility import static_rejection
from meshforge.controller.session.policies import PolicyIntake, hello_policy

pytestmark = pytest.mark.unit

GIB = 1024**3
OPEN = op.OPEN_POLICY


def _live() -> Any:
    return SimpleNamespace(
        node_id="node_x",
        session_id="s",
        policy=None,
        policy_refused=None,
        policy_slots=None,
    )


def test_hello_without_a_policy_is_an_older_agent() -> None:
    assert hello_policy(agent_pb2.Hello()) == (None, None)


def test_hello_with_a_valid_policy() -> None:
    policy = dataclasses.replace(OPEN, allowed_users=("alice",), max_ram_bytes=GIB)
    hello = agent_pb2.Hello(policy=op.to_proto(policy))
    assert hello_policy(hello) == (policy, None)


def test_hello_with_a_malformed_policy_fails_closed() -> None:
    msg = op.to_proto(OPEN)
    msg.allowed_users.append("Not-A-Name")
    policy, refusal = hello_policy(agent_pb2.Hello(policy=msg))
    assert policy == op.DENY_ALL and refusal


def test_a_valid_update_replaces_policy_and_slots() -> None:
    intake, live = PolicyIntake(), _live()
    update = agent_pb2.PolicyUpdate(
        policy=op.to_proto(OPEN), max_cpu_jobs=3, max_gpu_jobs=0
    )
    intake.on_update(live, update)
    assert live.policy == OPEN and live.policy_slots == (3, 0)
    assert live.policy_refused is None


def test_a_not_accepting_update_zeroes_the_slots() -> None:
    intake, live = PolicyIntake(), _live()
    intake.on_update(live, agent_pb2.PolicyUpdate(policy=op.to_proto(op.DENY_ALL)))
    assert live.policy == op.DENY_ALL and live.policy_slots == (0, 0)


@pytest.mark.parametrize(
    "update",
    [
        agent_pb2.PolicyUpdate(),  # no policy at all
        agent_pb2.PolicyUpdate(
            policy=agent_pb2.OwnerPolicy(accept_jobs=True, max_cpu_percent=0)
        ),
        agent_pb2.PolicyUpdate(policy=op.to_proto(OPEN), max_cpu_jobs=65),
        agent_pb2.PolicyUpdate(policy=op.to_proto(OPEN), max_gpu_jobs=1000),
    ],
)
def test_a_bad_update_fails_closed_and_is_counted(update: Any) -> None:
    intake, live = PolicyIntake(), _live()
    live.policy = OPEN
    intake.on_update(live, update)
    assert live.policy == op.DENY_ALL
    assert live.policy_refused
    assert live.policy_slots == (0, 0)
    assert intake.refused_total == 1


# ------------------------------------------------------------------ planner


def _node(policy: op.OwnerPolicy | None, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": frozenset({"exec", "exec-docker"}),
        "ram_bytes": 16 * GIB,
        "cpu_threads": 8,
        "cpu_slots": 1,
        "policy": policy,
    }
    base.update(kw)
    return NodeSnapshot(node_id="node_a", **base)


def _request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "owner_name": "alice",
    }
    base.update(kw)
    return PlanRequest(**base)


def test_no_policy_is_no_restriction() -> None:
    assert (
        static_rejection(_request(network=True, ram_bytes=99 * GIB // 10), _node(None))
        is None
    )


def test_docker_makes_a_job_sandboxable_even_when_unsandboxed_is_off() -> None:
    policy = dataclasses.replace(OPEN, allow_unsandboxed=False)
    assert static_rejection(_request(), _node(policy)) is None
    refused = static_rejection(
        _request(), _node(policy, capabilities=frozenset({"exec", "exec-subprocess"}))
    )
    assert refused is not None and refused.code is RejectCode.POLICY_UNSANDBOXED


def test_not_exec_comes_before_the_policy() -> None:
    refused = static_rejection(
        _request(), _node(op.DENY_ALL, capabilities=frozenset({"docker"}))
    )
    assert refused is not None and refused.code is RejectCode.NOT_EXEC


def _record(node_id: str) -> Any:
    return SimpleNamespace(
        node_id=node_id,
        state=NodeState.ONLINE,
        hardware={"capabilities": ["exec", "exec-docker"], "ram_bytes": str(16 * GIB)},
        hardware_scores=None,
        cpu_slots=1,
        gpu_slots=None,
    )


MANIFEST = Manifest.model_validate({"name": "x", "entrypoint": ["python", "m.py"]})


def test_submit_refuses_when_every_policy_refuses_the_submitter() -> None:
    nodes = [_record("node_a"), _record("node_b")]
    only_bob = dataclasses.replace(OPEN, allowed_users=("bob",))
    with pytest.raises(PlacementError) as exc:
        check_auto(
            MANIFEST,
            nodes,
            policies={"node_a": only_bob, "node_b": op.DENY_ALL},
            owner_name="alice",
        )
    codes = {r["reject_code"] for r in exc.value.details["rejections"]}
    assert codes == {"policy_user_not_allowed", "policy_not_accepting"}
    # bob may submit; a node with no known policy (disconnected) keeps the job.
    check_auto(MANIFEST, nodes, policies={"node_a": only_bob}, owner_name="bob")
    check_auto(MANIFEST, nodes, policies={"node_a": only_bob}, owner_name="alice")


def test_pinned_submit_names_the_policy_code() -> None:
    with pytest.raises(PlacementError) as exc:
        check_placement(
            Manifest.model_validate(
                {
                    "name": "x",
                    "entrypoint": ["python", "m.py"],
                    "limits": {"network": "on"},
                }
            ),
            _record("node_a"),
            connected=True,
            policy=OPEN.__class__(accept_jobs=True),
            owner_name="alice",
        )
    assert exc.value.details["reject_code"] == "policy_network"
