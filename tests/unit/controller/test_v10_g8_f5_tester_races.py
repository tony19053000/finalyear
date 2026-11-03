"""V1.0 G8 F5 tester: submit-time / plan-time agreement under rapid policy changes."""

from __future__ import annotations

import dataclasses
import random
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
HW: dict[str, Any] = {
    "capabilities": ["exec", "exec-subprocess", "exec-gpu", "exec-split"],
    "gpus": [{"index": 0, "vram_bytes": str(4 * GIB)}],
    "ram_bytes": str(16 * GIB),
    "cpu_threads": 8,
}


class _Live:
    node_id = "node_x"
    session_id = "sess_x"
    policy: op.OwnerPolicy | None = None
    policy_refused: str | None = None
    policy_slots: tuple[int | None, int | None] | None = None


def _m(**res: Any) -> Any:
    return parse_manifest_data({"entrypoint": "python a.py", "resources": res})


def _update(policy: op.OwnerPolicy, cpu: int = 1, gpu: int = 1) -> Any:
    u = agent_pb2.PolicyUpdate(policy=op.to_proto(policy))
    if policy.accept_jobs:
        u.max_cpu_jobs, u.max_gpu_jobs = cpu, gpu
    return u


def _submit(live: _Live, record: Any, manifest: Any) -> str | None:
    try:
        check_placement(
            manifest,
            record,
            connected=True,
            policy=live.policy,
            slots=live.policy_slots,
        )
    except PlacementError as exc:
        return str(exc.details.get("reject_code"))
    return None


def _plan(live: _Live, manifest: Any) -> str | None:
    snap = node_snapshot(
        "node_x",
        state="online",
        connected=True,
        hardware=HW,
        advertised_cpu_slots=None
        if live.policy_slots is None
        else live.policy_slots[0],
        advertised_gpu_slots=None
        if live.policy_slots is None
        else live.policy_slots[1],
        policy=live.policy,
    )
    req = plan_request(manifest, job_id="", attempt_no=1, env_key="e" * 64)
    r = static_rejection(req, snap)
    return None if r is None else r.code.value


def test_rapid_toggles_submit_check_and_planner_never_disagree() -> None:
    """Random PolicyUpdate stream (jobs on/off, GPU 0/1/2, bad updates); after
    every update the submit-time verdict equals the planner's static verdict
    and equals what the last update says."""
    rng = random.Random(1234)  # noqa: S311 - a seeded test sequence
    record = node_record(hardware=HW, cpu_slots=None, gpu_slots=None)
    live = _Live()
    live.policy = OFF
    intake = PolicyIntake()
    gpu_job = _m(gpu="required")
    for _ in range(500):
        kind = rng.choice(["on", "off", "on", "off", "bad", "nopolicy"])
        gpu = rng.choice([0, 1, 2])
        if kind == "on":
            intake.on_update(live, _update(ON, gpu=gpu))  # type: ignore[arg-type]
            want = "no_gpu_slots" if gpu == 0 else None
        elif kind == "off":
            intake.on_update(live, _update(OFF))  # type: ignore[arg-type]
            want = "policy_not_accepting"
        elif kind == "bad":
            intake.on_update(  # type: ignore[arg-type]
                live, agent_pb2.PolicyUpdate(policy=op.to_proto(ON), max_gpu_jobs=10**6)
            )
            want = "policy_not_accepting"
        else:
            intake.on_update(live, agent_pb2.PolicyUpdate())  # type: ignore[arg-type]
            want = "policy_not_accepting"
        assert _submit(live, record, gpu_job) == want
        assert _plan(live, gpu_job) == want


def test_deny_all_after_good_state_then_recovery() -> None:
    live = _Live()
    intake = PolicyIntake()
    record = node_record(hardware=HW, cpu_slots=1, gpu_slots=1)
    intake.on_update(live, _update(ON))  # type: ignore[arg-type]
    assert _submit(live, record, _m()) is None
    intake.on_update(live, agent_pb2.PolicyUpdate())  # type: ignore[arg-type]  # malformed
    assert live.policy == op.DENY_ALL
    assert _submit(live, record, _m()) == "policy_not_accepting"
    intake.on_update(live, _update(ON))  # type: ignore[arg-type]
    assert _submit(live, record, _m()) is None


def test_no_update_yet_falls_back_to_hello_slots_and_policy() -> None:
    """Race: submit lands between Hello and first PolicyUpdate."""
    record = node_record(hardware=HW, cpu_slots=1, gpu_slots=0)
    live = _Live()
    live.policy = ON  # Hello.policy
    assert live.policy_slots is None
    assert _submit(live, record, _m(gpu="required")) == "no_gpu_slots"
    assert _submit(live, record, _m()) is None


def test_auto_check_mixed_nodes_live_slots_decide_per_node() -> None:
    a = node_record(hardware=HW, cpu_slots=1, gpu_slots=1)
    b = node_record(hardware=HW, cpu_slots=1, gpu_slots=1)
    gpu = _m(gpu="required")
    pol = {a.node_id: ON, b.node_id: ON}
    # both turned GPU off live though their Hellos said 1
    with pytest.raises(PlacementError):
        check_auto(
            gpu, [a, b], policies=pol, slots={a.node_id: (1, 0), b.node_id: (1, 0)}
        )
    # one on -> some node could run it
    check_auto(gpu, [a, b], policies=pol, slots={a.node_id: (1, 0), b.node_id: (1, 1)})
    # a node with live=None entry falls back to the Hello's slots
    check_auto(gpu, [a, b], policies=pol, slots={a.node_id: (1, 0), b.node_id: None})
    # everybody's owner turned jobs off live
    with pytest.raises(PlacementError):
        check_auto(
            _m(),
            [a, b],
            policies={a.node_id: OFF, b.node_id: OFF},
            slots={a.node_id: (0, 0), b.node_id: (0, 0)},
        )


def test_jobs_off_beats_every_other_static_reason_for_a_connected_exec_node() -> None:
    """Exec present + policy off -> policy_not_accepting (not NOT_EXEC)."""
    req = plan_request(_m(), job_id="", attempt_no=1, env_key="e" * 64)
    snap = node_snapshot(
        "node_x", state="online", connected=True, hardware=HW, policy=OFF
    )
    r = static_rejection(req, snap)
    assert r is not None and r.code is RejectCode.POLICY_NOT_ACCEPTING


def test_exec_missing_message_matrix() -> None:
    no_exec = {**HW, "capabilities": ["hardware-scores"]}
    req = plan_request(_m(), job_id="", attempt_no=1, env_key="e" * 64)
    for policy, text in (
        (ON, NOT_EXEC_NO_RUNTIME),
        (OFF, NOT_EXEC_OWNER_OFF),
        (None, NOT_EXEC_OWNER_OFF),
        (op.DENY_ALL, NOT_EXEC_OWNER_OFF),
    ):
        snap = node_snapshot(
            "node_x", state="online", connected=True, hardware=no_exec, policy=policy
        )
        r = static_rejection(req, snap)
        assert r is not None and r.code is RejectCode.NOT_EXEC and r.text == text


def test_unsandboxed_only_node_with_allow_unsandboxed_off_is_policy_rejected() -> None:
    """exec-subprocess advertised (CAN) but owner disallows (ALLOWS): the planner
    must say policy_unsandboxed, not admit and not NOT_EXEC."""
    req = plan_request(_m(), job_id="", attempt_no=1, env_key="e" * 64)
    snap = node_snapshot(
        "node_x",
        state="online",
        connected=True,
        hardware=HW,
        policy=dataclasses.replace(ON, allow_unsandboxed=False),
    )
    r = static_rejection(req, snap)
    assert r is not None and r.code is RejectCode.POLICY_UNSANDBOXED
