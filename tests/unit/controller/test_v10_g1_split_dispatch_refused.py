"""V0.10 G1: a split job is accepted and stored, but until lanes exist (G4) the
dispatch planner refuses it typed - it must never run whole by accident
(``no_feasible_plan``, ``not_supported_yet: split execution arrives in G4``),
even with a perfectly good node online and even when pinned."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.states import JobState
from meshforge.controller.jobs.execution_store import Dispatch, DispatchFailure
from meshforge.controller.jobs.planning import SPLIT_NOT_SUPPORTED_YET, DispatchPlanner
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


def test_the_same_job_without_split_would_dispatch() -> None:
    node = str(new_node_id())
    assert isinstance(_planner(node).decide(_job(PLAIN), _fabric(node)), Dispatch)


def test_an_auto_split_job_is_refused_typed() -> None:
    node = str(new_node_id())
    decision = _planner(node).decide(_job(SPLIT), _fabric(node))
    assert isinstance(decision, DispatchFailure)
    assert decision.failure_type is FailureType.NO_FEASIBLE_PLAN
    assert decision.detail == SPLIT_NOT_SUPPORTED_YET
    assert SPLIT_NOT_SUPPORTED_YET.startswith("not_supported_yet: ")


def test_a_pinned_split_job_is_refused_too() -> None:
    node = str(new_node_id())
    decision = _planner(node).decide(_job(SPLIT, node_id=node), _fabric(node))
    assert isinstance(decision, DispatchFailure)
    assert decision.detail == SPLIT_NOT_SUPPORTED_YET


def test_the_split_column_alone_is_enough() -> None:
    node = str(new_node_id())
    record = SplitRecord(
        spec={"over": {"glob": "x"}}, items_digest="sha256:" + "b" * 64, item_count=3
    )
    decision = _planner(node).decide(_job(PLAIN, split=record), _fabric(node))
    assert isinstance(decision, DispatchFailure)
    assert decision.detail == SPLIT_NOT_SUPPORTED_YET
