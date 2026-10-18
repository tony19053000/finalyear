"""V0.9 G3 lane B item 4 (D215): a pinned job's dispatched decision carries the
planner's would-have-chosen node - a side-effect-free shadow plan of the same
request unpinned - and the run record's ``planner_node_id`` comes from it."""

from __future__ import annotations

import dataclasses
import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.states import JobState
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import Dispatch
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import PastFailure
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.telemetry.record import RecordSource, build_record

pytestmark = pytest.mark.unit

CAPS = ["exec", "exec-subprocess"]
JOB = {"name": "d", "entrypoint": ["python", "m.py"]}
A, B = "node_01AAAAAAAAAAAAAAAAAAAAAAAA", "node_01BBBBBBBBBBBBBBBBBBBBBBBB"
TREE = "sha256:" + "c" * 64


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
    def __init__(self) -> None:
        self.live = {A: _live(), B: _live()}

    def get(self, node_id: str) -> Any:
        return self.live.get(node_id)

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(policy: str = "cost_model") -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(default_policy=policy),
        clock=lambda: 1000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=1000.0,
    )


def _job(node_id: str | None, *, bench: bool = True) -> JobRecord:
    """A queued job; pinned to ``node_id`` when given. ``bench``: a campaign's
    forced run (its ``bench:<run_id>`` key) - since G4 item 0.8 only those get
    a shadow plan."""
    now = dt.datetime.now(dt.UTC)
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
        created_at=now,
        updated_at=now,
        scheduling=JobScheduling(input_bytes=2 * 1024**3, submitter_node_id=A),
    )


def _fabric() -> FabricRows:
    def row(node_id: str) -> NodeRow:
        return NodeRow(
            node_id=node_id,
            state="online",
            hardware={"capabilities": CAPS},
            hardware_scores=None,
            cpu_slots=2,
            gpu_slots=0,
        )

    # A already holds the 2 GiB project: the planner prefers it
    return FabricRows(nodes={A: row(A), B: row(B)}, holdings={A: frozenset({TREE})})


def test_auto_job_prefers_a_and_has_no_shadow() -> None:
    decision = _planner().decide(_job(None), _fabric())
    assert isinstance(decision, Dispatch) and decision.node_id == A
    assert decision.record is not None
    assert decision.record.shadow_chosen_node_id is None


def test_pinned_job_on_b_records_a_as_would_have_chosen() -> None:
    planner = _planner()
    fabric = _fabric()
    decision = planner.decide(_job(B), fabric)
    assert isinstance(decision, Dispatch) and decision.node_id == B
    record = decision.record
    assert record is not None
    assert record.chosen_node_id == B
    assert record.shadow_chosen_node_id == A
    assert record.shadow_estimate_s is not None and record.shadow_estimate_s > 0
    # the stored candidates stay the pinned plan's (one node): explain unchanged
    assert [c["node_id"] for c in record.candidates] == [B]


def test_shadow_has_no_side_effects_on_round_robin() -> None:
    planner = _planner("round_robin")
    fabric = _fabric()
    decision = planner.decide(_job(B), fabric)
    assert isinstance(decision, Dispatch)
    assert decision.record is not None and decision.record.shadow_chosen_node_id
    assert planner.round_robin_after == B  # the real dispatch moved it, the shadow not


def test_a_failing_shadow_never_blocks_the_pinned_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = planning_module.plan

    def plan(request: Any, *args: Any, **kwargs: Any) -> Any:
        if request.pinned_node_id is None:
            raise RuntimeError("shadow bug")
        return real(request, *args, **kwargs)

    monkeypatch.setattr(planning_module, "plan", plan)
    decision = _planner().decide(_job(B), _fabric())
    assert isinstance(decision, Dispatch) and decision.node_id == B
    assert decision.record is not None
    assert decision.record.shadow_chosen_node_id is None


def test_record_planner_node_id_comes_from_the_shadow_for_pinned_jobs() -> None:
    now = dt.datetime.now(dt.UTC)
    base: dict[str, Any] = {
        "attempt_id": str(new_attempt_id()),
        "job_id": str(new_job_id()),
        "attempt_no": 1,
        "node_id": B,
        "state": "done",
        "created_at": now,
        "submitter_node_id": A,
        "decision_chosen": B,
    }
    pinned = build_record(
        RecordSource(**base, placement={"node_id": B}, decision_shadow=A)
    )
    assert pinned["planner_node_id"] == A
    assert pinned["node_id"] == B and pinned["strategy"] == "mode1"
    assert pinned["placement_mode"] == "pinned"
    auto = build_record(RecordSource(**base, placement={}, decision_shadow=None))
    assert auto["planner_node_id"] == B


# ---------------------------------------------------- fix r1 (D-3, D235)


def test_fix1_d3_a_busy_preferred_node_is_still_the_preference() -> None:
    fabric = _fabric()
    fabric.used[A] = [2, 0]  # every CPU slot of A taken right now
    decision = _planner().decide(_job(B), fabric)
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.shadow_chosen_node_id == A


def test_fix1_d3_health_is_kept_an_unhealthy_preferred_node_is_not_chosen() -> None:
    planner = _planner()
    planner._manager.live.pop(A)  # type: ignore[attr-defined]  # A has no session
    decision = planner.decide(_job(B), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.shadow_chosen_node_id == B


def test_fix1_d3_feasibility_is_kept_a_node_without_cpu_slots_is_not_chosen() -> None:
    fabric = _fabric()
    fabric.nodes[A] = dataclasses.replace(fabric.nodes[A], cpu_slots=0)
    decision = _planner().decide(_job(B), fabric)
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.shadow_chosen_node_id == B


def test_fix1_d3_retry_exclusions_are_kept() -> None:
    fabric = _fabric()
    job = dataclasses.replace(_job(B), attempt_epoch=1)
    fabric.past_failures[job.id] = (
        PastFailure(
            node_id=A, failure_type="node_lost", ended_at=dt.datetime.now(dt.UTC)
        ),
    )
    decision = _planner().decide(job, fabric)
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.shadow_chosen_node_id == B  # A failed it: excluded
