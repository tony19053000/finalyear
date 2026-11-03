"""V1.0 G8 F5 (D428): what a node CAN run (``exec*`` capabilities) is separate
from what its owner ALLOWS (the live policy), on the controller side.

The lab sequence, pure: Hello with ``accept_jobs`` off -> ``PolicyUpdate`` on ->
admitted; on -> off -> refused; GPU slots changed live -> honoured at submit
(``check_placement`` / ``check_auto``) as in the planner.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from mf_api_records import node_record

from meshforge.common import owner_policy as op
from meshforge.common.manifest import parse_manifest_data
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.jobs.placement import (
    PlacementError,
    check_auto,
    check_placement,
)
from meshforge.controller.planner.feasibility import (
    NOT_EXEC_NO_RUNTIME,
    NOT_EXEC_OWNER_OFF,
    static_rejection,
)
from meshforge.controller.planner.inputs import node_snapshot, plan_request
from meshforge.controller.planner.types import RejectCode
from meshforge.controller.session.policies import PolicyIntake

pytestmark = pytest.mark.unit

GIB = 1024**3
OFF = dataclasses.replace(op.OPEN_POLICY, accept_jobs=False)
ON = op.OPEN_POLICY
HARDWARE: dict[str, Any] = {
    "capabilities": ["exec", "exec-subprocess", "exec-gpu", "exec-split"],
    "gpus": [{"index": 0, "vram_bytes": str(4 * GIB)}],
    "ram_bytes": str(16 * GIB),
    "cpu_threads": 8,
}
NO_EXEC: dict[str, Any] = {**HARDWARE, "capabilities": ["hardware-scores"]}


def _manifest(**resources: Any) -> Any:
    return parse_manifest_data({"entrypoint": "python a.py", "resources": resources})


def _request(**resources: Any) -> Any:
    return plan_request(
        _manifest(**resources), job_id="", attempt_no=1, env_key="e" * 64
    )


def _snapshot(
    policy: op.OwnerPolicy | None,
    *,
    hardware: dict[str, Any] = HARDWARE,
    slots: tuple[int | None, int | None] = (None, None),
) -> Any:
    return node_snapshot(
        "node_x",
        state="online",
        connected=True,
        hardware=hardware,
        advertised_cpu_slots=slots[0],
        advertised_gpu_slots=slots[1],
        policy=policy,
    )


class _Live:
    """The fields of ``LiveSession`` the policy intake writes."""

    node_id = "node_x"
    session_id = "sess_x"
    policy: op.OwnerPolicy | None = None
    policy_refused: str | None = None
    policy_slots: tuple[int | None, int | None] | None = None


def _update(policy: op.OwnerPolicy, cpu: int = 1, gpu: int = 1) -> Any:
    update = agent_pb2.PolicyUpdate(policy=op.to_proto(policy))
    if policy.accept_jobs:
        update.max_cpu_jobs, update.max_gpu_jobs = cpu, gpu
    return update


# ----------------------------------------------------------- the lab sequence


def test_off_at_hello_then_on_live_admits_and_off_again_refuses() -> None:
    live = _Live()
    live.policy = OFF  # Hello.policy at connect: Run jobs off
    request = _request()
    refused = static_rejection(request, _snapshot(live.policy))
    assert refused is not None and refused.code is RejectCode.POLICY_NOT_ACCEPTING

    intake = PolicyIntake()
    intake.on_update(live, _update(ON))  # type: ignore[arg-type]
    assert live.policy is not None and live.policy.accept_jobs
    assert static_rejection(request, _snapshot(live.policy, slots=(1, 1))) is None

    intake.on_update(live, _update(OFF))  # type: ignore[arg-type]
    assert live.policy_slots == (0, 0)
    refused = static_rejection(request, _snapshot(live.policy, slots=live.policy_slots))
    assert refused is not None and refused.code is RejectCode.POLICY_NOT_ACCEPTING


def test_a_bad_policy_update_fails_closed() -> None:
    live = _Live()
    live.policy = ON
    update = agent_pb2.PolicyUpdate(policy=op.to_proto(ON), max_cpu_jobs=10_000)
    PolicyIntake().on_update(live, update)  # type: ignore[arg-type]
    assert live.policy == op.DENY_ALL and live.policy_slots == (0, 0)
    refused = static_rejection(_request(), _snapshot(live.policy, slots=(0, 0)))
    assert refused is not None and refused.code is RejectCode.POLICY_NOT_ACCEPTING


def test_gpu_slots_follow_the_live_policy() -> None:
    gpu = _request(gpu="required")
    refused = static_rejection(gpu, _snapshot(ON, slots=(1, 0)))
    assert refused is not None and refused.code is RejectCode.NO_GPU_SLOTS
    assert static_rejection(gpu, _snapshot(ON, slots=(1, 1))) is None
    # the GPU capability is what the agent CAN do; slots are what it ALLOWS
    no_runtime = {**HARDWARE, "capabilities": ["exec", "exec-subprocess"]}
    refused = static_rejection(gpu, _snapshot(ON, hardware=no_runtime, slots=(1, 1)))
    assert refused is not None and refused.code is RejectCode.NO_GPU_RUNTIME


# ----------------------------------------------- no exec: the right reason


def test_no_exec_with_jobs_on_says_the_agent_has_no_runtime() -> None:
    """An older agent started with jobs off (no exec), whose owner turned jobs
    on live: it really has no runtime until its next Hello - refused, and the
    text says why (not "the owner has not enabled")."""
    refused = static_rejection(_request(), _snapshot(ON, hardware=NO_EXEC))
    assert refused is not None and refused.code is RejectCode.NOT_EXEC
    assert refused.text == NOT_EXEC_NO_RUNTIME
    assert "restart" in refused.text


@pytest.mark.parametrize("policy", [OFF, None])
def test_no_exec_otherwise_keeps_the_recorded_text(
    policy: op.OwnerPolicy | None,
) -> None:
    refused = static_rejection(_request(), _snapshot(policy, hardware=NO_EXEC))
    assert refused is not None and refused.code is RejectCode.NOT_EXEC
    assert refused.text == NOT_EXEC_OWNER_OFF  # golden decisions replay unchanged


# --------------------------------------------- submit-time checks use live slots


def test_pinned_check_uses_the_live_slots_over_the_hellos() -> None:
    # Hello sent with jobs off: no slots (None); the GPU switch was then set
    # live to "no GPU jobs" (0) - the submit check must see 0, not the default
    record = node_record(hardware=HARDWARE, cpu_slots=None, gpu_slots=None)
    gpu = _manifest(gpu="required")
    check_placement(gpu, record, connected=True, policy=ON)  # Hello's: default 1
    with pytest.raises(PlacementError) as refused:
        check_placement(gpu, record, connected=True, policy=ON, slots=(1, 0))
    assert refused.value.details["reject_code"] == RejectCode.NO_GPU_SLOTS.value
    # ... and the other way round: Hello said 0, the owner turned the GPU on
    zero = node_record(hardware=HARDWARE, cpu_slots=1, gpu_slots=0)
    with pytest.raises(PlacementError):
        check_placement(gpu, zero, connected=True, policy=ON)
    check_placement(gpu, zero, connected=True, policy=ON, slots=(1, 1))


def test_auto_check_uses_the_live_slots() -> None:
    zero = node_record(hardware=HARDWARE, cpu_slots=1, gpu_slots=0)
    gpu = _manifest(gpu="required")
    with pytest.raises(PlacementError):
        check_auto(gpu, [zero], policies={zero.node_id: ON})
    check_auto(gpu, [zero], policies={zero.node_id: ON}, slots={zero.node_id: (1, 1)})


def test_pinned_check_refuses_when_the_owner_turned_jobs_off_live() -> None:
    record = node_record(hardware=HARDWARE, cpu_slots=1, gpu_slots=1)
    check_placement(_manifest(), record, connected=True, policy=ON)
    with pytest.raises(PlacementError) as refused:
        check_placement(_manifest(), record, connected=True, policy=OFF, slots=(0, 0))
    assert refused.value.details["reject_code"] == RejectCode.POLICY_NOT_ACCEPTING.value
