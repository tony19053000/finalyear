"""V0.14 G1 (D292): the dispatcher stores a replay with every decision record,
and a replay never fails a dispatch.

* a dispatched, a waiting and a failed decision each carry a replay v1 that
  ``plan()`` turns back into the recorded decision (candidate JSON, digest,
  reason key, chosen node) - the dispatcher plans on the projection;
* a campaign's pinned run stores its shadow request, and the shadow replay
  reproduces the recorded would-have-chosen node;
* over ``replay_max_bytes`` -> no replay, counted, logged
  ``controller.jobs.replay_over_cap``, the job still dispatches;
* a codec bug -> no replay, counted, logged with its type, the job still
  dispatches;
* ``objective`` is ``min_time`` and ``calibration_version`` None (G2/G3 fill
  them).
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from meshforge.common.ids import new_job_id
from meshforge.common.states import JobState
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import Dispatch, DispatchFailure, Wait
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import (
    canonical_json,
    from_replay,
    shadow_from_replay,
)
from meshforge.controller.settings import JobApiSettings, PlannerSettings

pytestmark = pytest.mark.unit

CAPS = ["exec", "exec-subprocess"]
JOB = {"name": "d", "entrypoint": ["python", "m.py"]}
A, B = "node_01AAAAAAAAAAAAAAAAAAAAAAAA", "node_01BBBBBBBBBBBBBBBBBBBBBBBB"
TREE = "sha256:" + "c" * 64
OTHER = "sha256:" + "d" * 64
WALL = dt.datetime(2026, 10, 21, 9, 30, 0, 250000, tzinfo=dt.UTC)


def _live() -> Any:
    return SimpleNamespace(
        state=NodeState.ONLINE,
        rtt_ewma_ms=None,
        session_id="s",
        policy=None,
        policy_slots=None,
        clock=SimpleNamespace(latest=None),
    )


class _Manager:
    def __init__(self, nodes: tuple[str, ...] = (A, B)) -> None:
        self.live = {n: _live() for n in nodes}

    def get(self, node_id: str) -> Any:
        return self.live.get(node_id)

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(
    *, nodes: tuple[str, ...] = (A, B), max_bytes: int = 65_536
) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(replay_max_bytes=max_bytes),
        clock=lambda: 1000.0,
        wall_clock=lambda: WALL,
        started_at=0.0,
    )


def _job(node_id: str | None = None, *, bench: bool = False) -> JobRecord:
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest=JOB,
        bundle_digest=None,
        tree_id=TREE,
        placement={"mode": "offload", "node_id": node_id}
        if node_id
        else {"mode": "auto"},
        idempotency_key=f"bench:brun_{node_id}" if bench and node_id else None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=WALL,
        updated_at=WALL,
        scheduling=JobScheduling(input_bytes=2 * 1024**3, submitter_node_id=A),
    )


def _fabric(caps: list[str] = CAPS) -> FabricRows:
    def row(node_id: str) -> NodeRow:
        return NodeRow(
            node_id=node_id,
            state="online",
            hardware={"capabilities": caps},
            hardware_scores=None,
            cpu_slots=2,
            gpu_slots=0,
        )

    # A holds the project and another job's tree (the projection drops it)
    return FabricRows(
        nodes={A: row(A), B: row(B)}, holdings={A: frozenset({TREE, OTHER})}
    )


def _replays_to(record: Any) -> None:
    assert record.replay is not None and record.replay_version == 2  # V0.14 G2+G3 merge
    assert record.objective == "min_time" and record.calibration_version is None
    stored = json.loads(canonical_json(record.replay))  # as jsonb hands it back
    request, snapshot, now = from_replay(stored)
    assert now == WALL
    assert all(OTHER not in n.cached for n in snapshot.nodes)  # projected
    again = plan(request, snapshot, record.policy, now)
    assert candidates_json(again) == list(record.candidates)
    assert again.snapshot_digest == record.snapshot_digest
    assert again.reason_key == record.reason_key
    assert (None if again.chosen is None else again.chosen.node_id) == (
        record.chosen_node_id
    )


def test_dispatched_decision_carries_a_replay_of_itself() -> None:
    decision = _planner().decide(_job(), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    _replays_to(decision.record)


def test_waiting_decision_carries_a_replay_of_itself() -> None:
    planner = _planner(nodes=())  # nobody connected: every node no_session
    decision = planner.decide(_job(), _fabric())
    assert isinstance(decision, Wait) and decision.record is not None
    _replays_to(decision.record)


def test_failed_decision_carries_a_replay_of_itself() -> None:
    decision = _planner().decide(_job(), _fabric(caps=["docker"]))  # no exec
    assert isinstance(decision, DispatchFailure) and decision.record is not None
    _replays_to(decision.record)


def test_pinned_campaign_run_stores_the_shadow_request() -> None:
    decision = _planner().decide(_job(B, bench=True), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    record = decision.record
    assert record.shadow_chosen_node_id == A
    _replays_to(record)
    shadow = shadow_from_replay(json.loads(canonical_json(record.replay or {})))
    assert shadow is not None
    s_request, s_snapshot, s_now = shadow
    assert s_request.pinned_node_id is None and not s_request.benchmark
    again = plan(s_request, s_snapshot, record.policy, s_now)
    assert again.chosen is not None and again.chosen.node_id == A
    assert again.chosen.estimate is not None
    assert round(again.chosen.estimate.total_s, 6) == record.shadow_estimate_s


def test_a_user_pinned_job_has_no_shadow_in_its_replay() -> None:
    decision = _planner().decide(_job(B), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.replay is not None
    assert "shadow" not in decision.record.replay


def test_over_the_cap_the_job_dispatches_without_a_replay() -> None:
    planner = _planner(max_bytes=1024)
    with capture_logs() as logs:
        decision = planner.decide(_job(), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.replay is None and decision.record.replay_version is None
    assert decision.record.objective == "min_time"
    assert planner.replays_over_cap == 1 and planner.replay_failures == 0
    (event,) = [e for e in logs if e["event"] == "controller.jobs.replay_over_cap"]
    assert event["replay_bytes"] > 1024 and event["replay_max_bytes"] == 1024


def test_a_codec_bug_never_fails_the_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any, **_kw: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(planning_module, "bounded_replay", broken)
    planner = _planner()
    with capture_logs() as logs:
        decision = planner.decide(_job(), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.replay is None
    assert planner.replay_failures == 1 and planner.planning_errors == 0
    (event,) = [e for e in logs if e["event"] == "controller.jobs.replay_failed"]
    assert event["error_type"] == "RuntimeError" and event["job_id"]


def test_replay_max_bytes_is_validated() -> None:
    assert PlannerSettings().replay_max_bytes == 65_536
    with pytest.raises(ValueError, match="replay_max_bytes"):
        PlannerSettings(replay_max_bytes=10)
    with pytest.raises(ValueError, match="replay_max_bytes"):
        PlannerSettings(replay_max_bytes=10**9)
