"""Reviewer N-1 (V0.6 G3, item 0): only a job that is itself bad is failed by
the dispatch planner; any other planning error leaves the job QUEUED, counts
it and logs it (with its traceback) once per (job, error type)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.manifest import ManifestError
from meshforge.common.states import JobState
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import Dispatch, DispatchFailure, Wait
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.settings import JobApiSettings, PlannerSettings

CAPS = ["exec", "exec-subprocess"]
JOB = {"name": "d", "entrypoint": ["python", "m.py"]}


class _Manager:
    def __init__(self, node_id: str) -> None:
        self.live = {
            node_id: SimpleNamespace(
                state=NodeState.ONLINE, rtt_ewma_ms=None, session_id="s"
            )
        }

    def get(self, node_id: str) -> Any:
        return self.live.get(node_id)

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(node_id: str) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(node_id),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
        clock=lambda: 1000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=1000.0,
    )


def _job(manifest: dict[str, Any] | None = None) -> JobRecord:
    now = dt.datetime.now(dt.UTC)
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest=manifest or JOB,
        bundle_digest="sha256:" + "a" * 64,
        placement={"mode": "auto", "node_id": None},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
    )


def _fabric(node_id: str) -> FabricRows:
    return FabricRows(
        nodes={
            node_id: NodeRow(
                node_id=node_id,
                state="online",
                hardware={"capabilities": CAPS},
                hardware_scores=None,
                cpu_slots=1,
                gpu_slots=0,
            )
        }
    )


def _raising(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(planning_module, "plan_request", boom)


def test_a_stored_manifest_that_no_longer_validates_fails_the_job() -> None:
    node = str(new_node_id())
    planner = _planner(node)
    decision = planner.decide(_job({"name": 5}), _fabric(node))
    assert isinstance(decision, DispatchFailure)
    assert decision.failure_type is FailureType.INTERNAL_ERROR
    assert "ValidationError" in decision.detail
    assert planner.planning_errors == 0


@pytest.mark.parametrize(
    "exc",
    [ManifestError.one("entrypoint", "bad"), InvalidIdError("bad id")],
    ids=["manifest_error", "invalid_id"],
)
def test_other_job_is_bad_errors_fail_the_job(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    node = str(new_node_id())
    planner = _planner(node)
    _raising(monkeypatch, exc)
    decision = planner.decide(_job(), _fabric(node))
    assert isinstance(decision, DispatchFailure)
    assert decision.failure_type is FailureType.INTERNAL_ERROR
    assert type(exc).__name__ in decision.detail
    assert planner.planning_errors == 0


def test_any_other_error_leaves_the_job_queued_counted_and_logged_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = str(new_node_id())
    planner = _planner(node)
    _raising(monkeypatch, ZeroDivisionError("planner bug"))
    job = _job()
    with structlog.testing.capture_logs() as logs:
        first = planner.decide(job, _fabric(node))
        second = planner.decide(job, _fabric(node))
    assert isinstance(first, Wait) and first.record is None
    assert isinstance(second, Wait)
    assert planner.planning_errors == 2
    events = [e for e in logs if e["event"] == "controller.jobs.planning_error"]
    assert len(events) == 1, logs  # once per (job, error type), never per tick
    assert events[0]["job_id"] == job.id
    assert events[0]["error_type"] == "ZeroDivisionError"
    assert events[0]["exc_info"] is True
    assert not [e for e in logs if e["event"] == "controller.jobs.unplannable_job"]


def test_after_a_planning_error_the_next_job_still_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = str(new_node_id())
    planner = _planner(node)
    fabric = _fabric(node)
    real = planning_module.plan_request
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real(*args, **kwargs)

    monkeypatch.setattr(planning_module, "plan_request", flaky)
    assert isinstance(planner.decide(_job(), fabric), Wait)
    assert isinstance(planner.decide(_job(), fabric), Dispatch)
