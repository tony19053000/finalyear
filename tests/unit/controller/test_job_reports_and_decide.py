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
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchFailure,
    NodeView,
)
from meshforge.controller.jobs.progress import MAX_PHASE_S
from meshforge.controller.jobs.reports import (
    FAILURE_MESSAGE_MAX_LENGTH,
    MAX_MISSING_OUTPUTS,
    MISSING_OUTPUT_MAX_LENGTH,
    parse_status,
)
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.settings import JobApiSettings

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


def _node(node_id: str, caps: list[str], active: int = 0, gpus: Any = ()) -> NodeView:
    return NodeView(
        node_id=node_id,
        state="online",
        hardware={"capabilities": caps, "gpus": list(gpus)},
        hardware_scores=None,
        active_attempts=active,
    )


def _dispatcher(manager: FakeManager, clock: Clock) -> Dispatcher:
    return Dispatcher(
        store=None,  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=None,  # type: ignore[arg-type]
        settings=JobApiSettings(),
        clock=clock,
        blob_port=lambda: 1,
    )


def test_decide_rules() -> None:
    manager, clock = FakeManager(), Clock()
    d = _dispatcher(manager, clock)
    node = str(new_node_id())
    caps = ["exec", "exec-subprocess"]
    # just started, node not back yet: wait (agents are reconnecting)
    assert d._decide(_job(node), None) is None
    clock.now += JobApiSettings().reconnect_grace_s + 1
    gone = d._decide(_job(node), None)
    assert (
        isinstance(gone, DispatchFailure) and gone.failure_type is FailureType.NODE_LOST
    )
    # SUSPECT (lingering): wait, never fail on a blip
    manager.lingering[node] = object()
    assert d._decide(_job(node), None) is None
    del manager.lingering[node]
    live = SimpleNamespace(state=NodeState.SUSPECT, rtt_ewma_ms=None)
    manager.live[node] = live
    assert d._decide(_job(node), _node(node, caps)) is None
    live.state = NodeState.ONLINE
    refused = d._decide(_job(node), _node(node, ["docker"]))
    assert isinstance(refused, DispatchFailure)
    assert refused.failure_type is FailureType.NO_FEASIBLE_PLAN
    no_gpu = d._decide(_job(node, gpu="required"), _node(node, caps))
    assert isinstance(no_gpu, DispatchFailure)
    assert d._decide(_job(node), _node(node, caps, active=1)) is None  # busy
    go = d._decide(_job(node), _node(node, caps))
    assert isinstance(go, Dispatch)
    assert go.start_snapshot["gpu_indices"] == []
    gpus = [{"index": 0, "vram_bytes": "4"}, {"index": 1, "vram_bytes": "8"}]
    with_gpu = d._decide(
        _job(node, gpu="preferred"), _node(node, [*caps, "exec-gpu"], gpus=gpus)
    )
    assert isinstance(with_gpu, Dispatch)
    assert with_gpu.start_snapshot["gpu_indices"] == [1]  # the largest GPU
