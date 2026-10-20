"""V0.10 G4 (was G1's refusal test): a split job is dispatched as LANES - never
as a whole-job ``Dispatch``. A node whose agent has no ``exec-split`` never gets
a lane (``no_split_support``, static -> ``no_feasible_plan`` when no node can);
a pinned split job runs one lane on its node; a job row whose manifest lost its
``split:`` (stored-data fault) is failed, never run whole."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.states import JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchFailure,
    DispatchLanes,
)
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, SplitRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.settings import JobApiSettings, PlannerSettings

CAPS = ["exec", "exec-subprocess"]
PLAIN = {"name": "d", "entrypoint": ["python", "m.py"]}
SPLIT = {**PLAIN, "split": {"over": {"glob": "data/*"}}}


class _Manager:
    def __init__(self, node_id: str) -> None:
        self.live = {
            node_id: SimpleNamespace(
                state=NodeState.ONLINE,
                rtt_ewma_ms=None,
                session_id="s",
                policy=None,
                policy_slots=None,
                clock=SimpleNamespace(latest=None),
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


def _job(
    manifest: dict[str, Any],
    *,
    node_id: str | None = None,
    split: SplitRecord | None = None,
) -> JobRecord:
    now = dt.datetime.now(dt.UTC)
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest=manifest,
        bundle_digest=None,
        tree_id="sha256:" + "a" * 64,
        placement={"mode": "offload" if node_id else "auto", "node_id": node_id},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
        split=split,
    )


def _fabric(node_id: str, caps: list[str] | None = None) -> FabricRows:
    return FabricRows(
        nodes={
            node_id: NodeRow(
                node_id=node_id,
                state="online",
                hardware={"capabilities": caps or CAPS},
                hardware_scores=None,
                cpu_slots=1,
                gpu_slots=0,
            )
        }
    )


def test_the_same_job_without_split_would_dispatch() -> None:
    node = str(new_node_id())
    assert isinstance(_planner(node).decide(_job(PLAIN), _fabric(node)), Dispatch)


RECORD = SplitRecord(
    spec={"over": {"glob": "data/*"}}, items_digest="sha256:" + "b" * 64, item_count=8
)


def test_an_agent_without_exec_split_never_gets_a_lane() -> None:
    node = str(new_node_id())
    decision = _planner(node).decide(_job(SPLIT, split=RECORD), _fabric(node))
    assert isinstance(decision, DispatchFailure)
    assert decision.failure_type is FailureType.NO_FEASIBLE_PLAN
    assert decision.record is not None
    codes = {c.get("reject_code") for c in decision.record.candidates}
    assert "no_split_support" in codes


def test_an_auto_split_job_runs_as_lanes() -> None:
    node = str(new_node_id())
    decision = _planner(node).decide(
        _job(SPLIT, split=RECORD), _fabric(node, [*CAPS, "exec-split"])
    )
    assert isinstance(decision, DispatchLanes), decision
    assert [lane.node_id for lane in decision.lanes] == [node]  # one node: one lane
    assert 1 <= decision.chunk_items <= 8
    assert decision.queue_policy.max_failed_sites == 2


def test_a_pinned_split_job_is_one_lane_on_its_node() -> None:
    node = str(new_node_id())
    decision = _planner(node).decide(
        _job(SPLIT, node_id=node, split=RECORD), _fabric(node, [*CAPS, "exec-split"])
    )
    assert isinstance(decision, DispatchLanes), decision
    assert [lane.node_id for lane in decision.lanes] == [node]


def test_the_split_column_alone_never_runs_whole() -> None:
    node = str(new_node_id())
    decision = _planner(node).decide(
        _job(PLAIN, split=RECORD), _fabric(node, [*CAPS, "exec-split"])
    )
    assert isinstance(decision, DispatchFailure)
    assert decision.failure_type is FailureType.INTERNAL_ERROR
