"""V0.14 G3 - the dispatcher's side: objective resolution (placement >
manifest > default), the objective in the record and the replay, the nodes'
reliability in the snapshot (risk window 0 = none), the G3 planner config,
and the decision-latency budget counter/log."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from meshforge.common.ids import new_job_id
from meshforge.common.manifest import Manifest
from meshforge.common.states import JobState
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import Dispatch
from meshforge.controller.jobs.planning import DispatchPlanner, job_objective
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import from_replay
from meshforge.controller.planner.risk import RISK_TERM
from meshforge.controller.planner.types import Reliability
from meshforge.controller.settings import JobApiSettings, PlannerSettings, SplitSettings

pytestmark = pytest.mark.unit

TREE = "sha256:" + "c" * 64
WALL = dt.datetime(2026, 10, 21, 9, 30, tzinfo=dt.UTC)
IDS = [f"node_01{i:024d}" for i in range(3)]


class _Manager:
    def get(self, node_id: str) -> Any:
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
        wall_clock=lambda: WALL,
        started_at=0.0,
        split=SplitSettings(visibility_timeout_s=45.0),
    )


def _job(placement: dict[str, Any] | None = None, **manifest: Any) -> JobRecord:
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest={"name": "d", "entrypoint": ["python", "m.py"], **manifest},
        bundle_digest=None,
        tree_id=TREE,
        placement={"mode": "auto", **(placement or {})},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=WALL,
        updated_at=WALL,
        scheduling=JobScheduling(input_bytes=10**6, submitter_node_id=IDS[0]),
    )


def _fabric(reliability: dict[str, tuple[int, int]] | None = None) -> FabricRows:
    return FabricRows(
        nodes={
            n: NodeRow(
                node_id=n,
                state="online",
                hardware={"capabilities": ["exec", "exec-subprocess"]},
                hardware_scores=None,
                cpu_slots=2,
                gpu_slots=0,
            )
            for n in IDS
        },
        reliability=dict(reliability or {}),
    )


# ---------------------------------------------------------------- objective


@pytest.mark.parametrize(
    ("placement", "manifest", "default", "want"),
    [
        ({}, {}, "min_time", "min_time"),
        ({}, {}, "prefer_local", "prefer_local"),
        ({}, {"objective": "min_network"}, "prefer_local", "min_network"),
        (
            {"objective": "prefer_local"},
            {"objective": "min_network"},
            "min_time",
            "prefer_local",
        ),
        (
            {"objective": "nonsense"},
            {"objective": "min_network"},
            "min_time",
            "min_network",
        ),
        ({"objective": 7}, {}, "prefer_local", "prefer_local"),
    ],
)
def test_objective_resolution(
    placement: dict[str, Any], manifest: dict[str, Any], default: str, want: str
) -> None:
    job = _job(placement, **manifest)
    parsed = Manifest.model_validate(dict(job.manifest))
    assert job_objective(job, parsed, default) == want


def test_the_record_and_the_replay_carry_the_resolved_objective() -> None:
    planner = _planner(default_objective="prefer_local")
    for job, want in (
        (_job(), "prefer_local"),
        (_job(objective="min_network"), "min_network"),
        (_job({"objective": "min_time"}, objective="min_network"), "min_time"),
    ):
        decision = planner.decide(job, _fabric())
        assert isinstance(decision, Dispatch) and decision.record is not None
        assert decision.record.objective == want
        assert decision.record.replay is not None
        request, snapshot, now = from_replay(decision.record.replay)
        assert request.objective == want
        again = plan(request, snapshot, decision.record.policy, now)
        assert again.chosen is not None
        assert again.chosen.node_id == decision.record.chosen_node_id
        assert again.reason_key == decision.record.reason_key


# ---------------------------------------------------------------- reliability


def test_each_node_carries_its_reliability_and_the_risk_term_shows() -> None:
    planner = _planner(risk_window_days=3.0)
    fabric = _fabric({IDS[1]: (2, 10)})
    snapshot = planner.snapshot(fabric)
    by_id = {n.node_id: n.reliability for n in snapshot.nodes}
    assert by_id[IDS[1]] == Reliability(failures=2, total=10, window_days=3.0)
    assert by_id[IDS[0]] == Reliability(failures=0, total=0, window_days=3.0)
    decision = planner.decide(_job(), _fabric({IDS[1]: (2, 10)}))
    assert isinstance(decision, Dispatch) and decision.record is not None
    rows = decision.record.candidates
    assert all(RISK_TERM in r["estimate"]["terms"] for r in rows if r["estimate"])


def test_a_risk_window_of_zero_means_no_reliability_and_no_risk_term() -> None:
    planner = _planner(risk_window_days=0.0)
    snapshot = planner.snapshot(_fabric({IDS[1]: (2, 10)}))
    assert all(n.reliability is None for n in snapshot.nodes)
    decision = planner.decide(_job(), _fabric({IDS[1]: (2, 10)}))
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert all(
        RISK_TERM not in r["estimate"]["terms"]
        for r in decision.record.candidates
        if r["estimate"]
    )


def test_the_planner_config_carries_the_g3_settings() -> None:
    planner = _planner(
        risk_prior=(2.0, 30.0),
        prefer_local_margin=0.4,
        min_useful_bw_bytes_s=2e6,
        min_useful_bytes=1024,
    )
    config = planner.config
    assert config.risk.prior == (2.0, 30.0)
    assert config.risk.lease_release_s == 45.0  # [controller.split] visibility
    assert config.prefer_local_margin == 0.4
    assert config.min_useful_bw_bytes_s == 2e6 and config.min_useful_bytes == 1024
    assert config.prune_dominated is True


# ---------------------------------------------------------------- latency


def test_a_decision_over_the_latency_budget_is_counted_and_logged_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter(float(i) for i in range(1000))  # every reading 1 s later
    monkeypatch.setattr(planning_module.time, "perf_counter", lambda: next(ticks))
    planner = _planner(decision_latency_budget_ms=100.0)
    job = _job()
    with capture_logs() as logs:
        for _ in range(3):
            planner.decide(job, _fabric())
    assert planner.decisions_over_budget == 3
    events = [e for e in logs if e["event"] == "controller.jobs.decision_over_budget"]
    assert len(events) == 1 and events[0]["job_id"] == job.id
    assert events[0]["decision_ms"] == 1000.0 and events[0]["budget_ms"] == 100.0


def test_a_decision_within_the_budget_is_not_counted() -> None:
    planner = _planner(decision_latency_budget_ms=60_000.0)
    planner.decide(_job(), _fabric())
    assert planner.decisions_over_budget == 0
