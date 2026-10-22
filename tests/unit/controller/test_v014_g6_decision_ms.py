"""V0.14 G6 (D324): the dispatcher stamps its decision's wall time on the
decision row it hands the store - whole job and split, every outcome that
carries a row; an outcome without a row stays as it is."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.states import JobState
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import (
    DecisionRecord,
    Dispatch,
    DispatchFailure,
    DispatchLanes,
    Wait,
)
from meshforge.controller.jobs.planning import DispatchPlanner, with_decision_ms
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, SplitRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.settings import JobApiSettings, PlannerSettings

pytestmark = pytest.mark.unit

CAPS = ["exec", "exec-subprocess"]
PLAIN = {"name": "d", "entrypoint": ["python", "m.py"]}
SPLIT = {**PLAIN, "split": {"over": {"glob": "data/*"}}}
SPLIT_ROW = SplitRecord(
    spec={"over": {"glob": "data/*"}}, items_digest="sha256:" + "b" * 64, item_count=8
)
RECORD = DecisionRecord(
    policy="cost_model", snapshot_digest="d", reason_key="r", candidates=()
)


class _Manager:
    def get(self, _node_id: str) -> Any:
        return SimpleNamespace(
            state=NodeState.ONLINE,
            rtt_ewma_ms=None,
            session_id="s",
            policy=None,
            policy_slots=None,
            clock=SimpleNamespace(latest=None),
        )

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(**settings: Any) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(**settings),
        clock=lambda: 1000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=1000.0,
    )


def _job(manifest: dict[str, Any], *, split: SplitRecord | None = None) -> JobRecord:
    now = dt.datetime.now(dt.UTC)
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest=manifest,
        bundle_digest=None,
        tree_id="sha256:" + "a" * 64,
        placement={"mode": "auto"},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
        split=split,
    )


def _fabric(caps: list[str], n: int = 2) -> FabricRows:
    ids = sorted(str(new_node_id()) for _ in range(n))
    return FabricRows(
        nodes={
            i: NodeRow(
                node_id=i,
                state="online",
                hardware={"capabilities": caps},
                hardware_scores=None,
                cpu_slots=1,
                gpu_slots=0,
            )
            for i in ids
        }
    )


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``perf_counter`` reading 12.5 ms after the one before it."""
    clock = iter(i * 0.0125 for i in range(10_000))
    monkeypatch.setattr(planning_module.time, "perf_counter", lambda: next(clock))


@pytest.mark.usefixtures("ticks")
def test_a_whole_job_dispatch_carries_its_decision_ms() -> None:
    decision = _planner().decide(_job(PLAIN), _fabric(CAPS))
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.decision_ms == 12.5


@pytest.mark.usefixtures("ticks")
def test_a_split_dispatch_carries_its_decision_ms() -> None:
    decision = _planner().decide(
        _job(SPLIT, split=SPLIT_ROW), _fabric([*CAPS, "exec-split"])
    )
    assert isinstance(decision, DispatchLanes) and decision.record is not None
    assert decision.record.decision_ms == 12.5


@pytest.mark.usefixtures("ticks")
def test_a_failed_decision_with_a_row_carries_its_decision_ms() -> None:
    # no node has exec-split: every candidate is statically rejected
    decision = _planner().decide(_job(SPLIT, split=SPLIT_ROW), _fabric(CAPS))
    assert isinstance(decision, DispatchFailure)
    assert decision.failure_type is FailureType.NO_FEASIBLE_PLAN
    assert decision.record is not None and decision.record.decision_ms == 12.5


def test_the_real_clock_gives_a_small_non_negative_number() -> None:
    decision = _planner().decide(_job(PLAIN), _fabric(CAPS))
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.decision_ms is not None
    assert 0.0 <= decision.record.decision_ms < 60_000.0


@pytest.mark.usefixtures("ticks")
def test_a_planning_error_waits_without_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    planner = _planner()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("planner bug")

    monkeypatch.setattr(planner, "_decide", boom)
    decision = planner.decide(_job(PLAIN), _fabric(CAPS))
    assert decision == Wait()


@pytest.mark.usefixtures("ticks")
def test_the_over_budget_counter_still_counts() -> None:
    planner = _planner(decision_latency_budget_ms=10.0)
    decision = planner.decide(_job(PLAIN), _fabric(CAPS))
    assert planner.decisions_over_budget == 1
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.decision_ms == 12.5


@pytest.mark.parametrize(
    "outcome",
    [
        Wait(RECORD),
        DispatchFailure(FailureType.NO_FEASIBLE_PLAN, "x", RECORD),
        Dispatch(attempt_id="a", start_snapshot={}, record=RECORD),
    ],
)
def test_with_decision_ms_stamps_every_outcome_with_a_row(outcome: Any) -> None:
    stamped = with_decision_ms(outcome, 3.14159)
    assert type(stamped) is type(outcome)
    assert stamped.record.decision_ms == 3.142  # 3 decimals
    assert outcome.record.decision_ms is None  # frozen: the input is untouched


@pytest.mark.parametrize(
    "outcome",
    [None, Wait(), DispatchFailure(FailureType.INTERNAL_ERROR, "bad job")],
)
def test_with_decision_ms_leaves_an_outcome_without_a_row(outcome: Any) -> None:
    assert with_decision_ms(outcome, 5.0) == outcome


def test_with_decision_ms_is_never_negative() -> None:
    stamped = with_decision_ms(Wait(RECORD), -0.5)
    assert isinstance(stamped, Wait) and stamped.record is not None
    assert stamped.record.decision_ms == 0.0
