"""V0.4 G4: AttemptStatus bounds (hostile input) and the dispatch decision (D65)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchFailure,
    Wait,
)
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.progress import MAX_PHASE_S
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.reports import (
    FAILURE_MESSAGE_MAX_LENGTH,
    MAX_MISSING_OUTPUTS,
    MISSING_OUTPUT_MAX_LENGTH,
    parse_status,
)
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.settings import JobApiSettings, PlannerSettings

pytestmark = pytest.mark.unit


def _status(**kw: Any) -> agent_pb2.AttemptStatus:
    base: dict[str, Any] = {
        "job_id": str(new_job_id()),
        "attempt_id": str(new_attempt_id()),
        "fencing_token": 1,
        "state": common_pb2.ATTEMPT_STATE_RUNNING,
    }
    base.update(kw)
    return agent_pb2.AttemptStatus(**base)


def _failed(message: str) -> agent_pb2.AttemptStatus:
    return _status(
        state=common_pb2.ATTEMPT_STATE_FAILED,
        failure=common_pb2.Failure(
            failure_type=common_pb2.FAILURE_TYPE_USER_ERROR, message=message
        ),
    )


def test_a_valid_terminal_report_parses() -> None:
    status = _status(state=common_pb2.ATTEMPT_STATE_DONE, exit_code=0, runtime="docker")
    status.phases.run_s = 1.5
    report = parse_status(status)
    assert not isinstance(report, str)
    assert report.state is AttemptState.DONE and report.exit_code == 0
    assert report.phases is not None and report.phases.run_s == 1.5
    assert report.phases.stage_s is None  # absent = never ran


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (_status(attempt_id="nope"), "canonical"),
        (_status(fencing_token=0), "fencing_token"),
        (_status(state=0), "state"),
        (_status(state=common_pb2.ATTEMPT_STATE_LOST), "not an agent state"),
        (_status(state=common_pb2.ATTEMPT_STATE_FAILED), "without a failure"),
        (
            _status(failure=common_pb2.Failure(failure_type=1)),
            "not FAILED",
        ),
        (
            _status(
                state=common_pb2.ATTEMPT_STATE_FAILED,
                failure=common_pb2.Failure(failure_type=0),
            ),
            "failure_type",
        ),
        (_failed("x" * (FAILURE_MESSAGE_MAX_LENGTH + 1)), "failure.message"),
        (_failed("bad\x1b[31m"), "U+001B"),
        (_status(output_digest="sha256:xyz"), "output_digest"),
        (
            _status(missing_outputs=["a"] * (MAX_MISSING_OUTPUTS + 1)),
            "missing_outputs",
        ),
        (
            _status(missing_outputs=["m" * (MISSING_OUTPUT_MAX_LENGTH + 1)]),
            "missing_outputs[0]",
        ),
        (_status(runtime="kubernetes"), "runtime"),
        (_status(signal=65), "signal"),
    ],
)
def test_each_bound_refuses(status: agent_pb2.AttemptStatus, needle: str) -> None:
    reason = parse_status(status)
    assert isinstance(reason, str) and needle in reason
    assert "\x1b" not in reason


def test_bounds_accept_their_maximum() -> None:
    assert not isinstance(parse_status(_failed("x" * FAILURE_MESSAGE_MAX_LENGTH)), str)
    ok = _status(
        missing_outputs=["m" * MISSING_OUTPUT_MAX_LENGTH] * MAX_MISSING_OUTPUTS,
        signal=64,
    )
    assert not isinstance(parse_status(ok), str)
    at_max = _status()
    at_max.phases.run_s = MAX_PHASE_S
    assert not isinstance(parse_status(at_max), str)
    over = _status()
    over.phases.run_s = MAX_PHASE_S * 2
    assert "phases.run_s" in str(parse_status(over))
    nan = _status()
    nan.phases.env_s = float("nan")
    assert "phases.env_s" in str(parse_status(nan))


# ------------------------------------------------------------------ decide


class FakeManager:
    def __init__(self) -> None:
        self.live: dict[str, Any] = {}
        self.lingering: dict[str, Any] = {}
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)

    def get(self, node_id: str) -> Any:
        return self.live.get(node_id)

    def get_lingering(self, node_id: str) -> Any:
        return self.lingering.get(node_id)

    def defer_ack(self, _p: str) -> None:
        return None


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def _job(node_id: str, gpu: str = "none") -> JobRecord:
    now = dt.datetime.now(dt.UTC)
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest={
            "name": "demo",
            "entrypoint": ["python", "main.py"],
            "resources": {"gpu": gpu},
        },
        bundle_digest="sha256:" + "a" * 64,
        placement={"mode": "offload", "node_id": node_id},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
    )


def _fabric(
    node_id: str, caps: list[str], active: int = 0, gpus: Any = ()
) -> FabricRows:
    return FabricRows(
        nodes={
            node_id: NodeRow(
                node_id=node_id,
                state="online",
                hardware={"capabilities": caps, "gpus": list(gpus)},
                hardware_scores=None,
                cpu_slots=None,
                gpu_slots=None,
            )
        },
        used={node_id: [active, 0]},
    )


def _planner(manager: FakeManager, clock: Clock) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
        clock=clock,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=clock(),
    )


def _live(state: NodeState) -> SimpleNamespace:
    return SimpleNamespace(state=state, rtt_ewma_ms=None, session_id="s")


def test_pinned_decide_rules_keep_v04_behaviour() -> None:
    """D65/D103: a pinned job keeps V0.4's outcomes, now via the planner."""
    manager, clock = FakeManager(), Clock()
    d = _planner(manager, clock)
    node = str(new_node_id())
    caps = ["exec", "exec-subprocess"]
    empty = FabricRows(nodes={})
    # just started, node not back yet: wait (agents are reconnecting)
    assert isinstance(d.decide(_job(node), empty), Wait)
    clock.now += JobApiSettings().reconnect_grace_s + 1
    gone = d.decide(_job(node), empty)
    assert (
        isinstance(gone, DispatchFailure) and gone.failure_type is FailureType.NODE_LOST
    )
    assert gone.detail == "placed_node_offline"
    # SUSPECT (lingering): wait, never fail on a blip
    manager.lingering[node] = object()
    lingering = d.decide(_job(node), _fabric(node, caps))
    assert isinstance(lingering, Wait) and lingering.record is not None
    del manager.lingering[node]
    manager.live[node] = _live(NodeState.SUSPECT)
    assert isinstance(d.decide(_job(node), _fabric(node, caps)), Wait)
    manager.live[node].state = NodeState.ONLINE
    deleted = d.decide(_job(node), empty)
    assert isinstance(deleted, DispatchFailure)
    assert deleted.detail == "placed_node_deleted"
    refused = d.decide(_job(node), _fabric(node, ["docker"]))
    assert isinstance(refused, DispatchFailure)
    assert refused.failure_type is FailureType.NO_FEASIBLE_PLAN
    assert refused.detail == "node_no_longer_accepts_jobs"
    no_gpu = d.decide(_job(node, gpu="required"), _fabric(node, caps))
    assert isinstance(no_gpu, DispatchFailure)
    assert no_gpu.detail == "node_can_no_longer_provide_a_gpu"
    busy = d.decide(_job(node), _fabric(node, caps, active=1))  # 1 CPU slot used
    assert isinstance(busy, Wait) and busy.record is not None
    go = d.decide(_job(node), _fabric(node, caps))
    assert isinstance(go, Dispatch)
    assert go.node_id == node and go.slot == "cpu"
    assert go.start_snapshot["gpu_indices"] == []
    assert go.record is not None and go.record.chosen_node_id == node
    gpus = [{"index": 0, "vram_bytes": "4"}, {"index": 1, "vram_bytes": "8"}]
    with_gpu = d.decide(
        _job(node, gpu="preferred"), _fabric(node, [*caps, "exec-gpu"], gpus=gpus)
    )
    assert isinstance(with_gpu, Dispatch) and with_gpu.slot == "gpu"
    assert with_gpu.start_snapshot["gpu_indices"] == [1]  # the largest GPU
