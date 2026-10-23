"""V0.15 G4 tester round 1, on a real PostgreSQL: the ML decision loop under
model changes, failing models and the calibration read-back.

Everything is read back from ``jsonb`` (what the dashboard / CLI / the
trainers see), through the real ``ExecutionStore.dispatch_planned`` and the
real ``DispatchPlanner``:

* **feedback (D362) on PG:** attempts ended ``done`` after ML-driven dispatches
  are matched by the V0.14 calibration read (``read_outcomes`` + ``match``):
  the predicted seconds are the analytical raw (60 s), never the ML-corrected
  ones, and the fitted factor is ``actual / raw``;
* **model changes between ticks:** model A -> no model -> model B: every row
  names the model it was planned with, a V0.14-shaped row appears when there
  is no model, each stored replay reproduces with *its* model and is
  ``ml_model_unavailable`` with another;
* **D363 bookkeeping:** every failing decision is counted, the error is logged
  at most once per 60 s (and again after);
* a batch of jobs in one tick all use the one model of the tick;
* a failing model never leaves a job queued: the dispatch happens.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

import structlog
from alembic import command
from mf_ml_stub import Rule, stub_ml
from sqlalchemy import text

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.controller.calibration_store import _match_all, read_outcomes
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.ml.replay import ML_MODEL_UNAVAILABLE, replay_decision
from meshforge.controller.planner.calibration import CalibrationConfig, fit
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import MlInput
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MANIFEST = {"name": "g4r1", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "ab" * 32


@dataclass
class Live:
    node_id: str
    session_id: str
    state: NodeState = NodeState.ONLINE
    rtt_ewma_ms: float | None = 1.0
    policy: Any = None
    policy_slots: Any = None
    clock: Any = field(default_factory=lambda: SimpleNamespace(latest=None))


class Manager:
    def __init__(self, nodes: list[str]) -> None:
        self.lives = {n: Live(n, str(new_session_id())) for n in nodes}

    def get(self, node_id: str) -> Live | None:
        return self.lives.get(node_id)

    def get_lingering(self, node_id: str) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _nodes(pg: DatabaseSettings, n: int) -> list[str]:
    engine = create_engine(pg)
    try:
        out = []
        for _ in range(n):
            node = str(new_node_id())
            await Registry(engine).upsert_node_on_hello(
                node_id=node,
                hostname="h",
                os="linux",
                platform="x86_64",
                hardware={"capabilities": ["exec", "exec-subprocess"]},
                session_epoch=1,
                session_id=str(new_session_id()),
                state=NodeState.ONLINE,
                hardware_scores=None,
                cpu_slots=16,
                gpu_slots=0,
            )
            out.append(node)
        return sorted(out)
    finally:
        await engine.dispose()


def _planner(manager: Manager, clock: Clock | None = None) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=60.0, default_env_s=20.0),
        clock=clock or (lambda: 1_000_000.0),
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def _queue(store: ExecutionStore, fp: str) -> str:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g4r1",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint=fp, input_bytes=0),
    )
    return str(job.id)


async def _tick(store: ExecutionStore, planner: DispatchPlanner) -> dict[str, str]:
    """One dispatch tick; ``job id -> attempt id`` of what was dispatched."""
    results = await store.dispatch_planned(
        planner.decide,
        limit=50,
        max_per_node=64,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )
    return {str(r.job.id): r.attempt.id for r in results if r.attempt is not None}


async def _row(pg: DatabaseSettings, attempt: str) -> Any:
    (row,) = await _sql(
        pg,
        "SELECT policy, chosen_node_id, snapshot_digest, reason_key, candidates, "
        "replay FROM plan_decision WHERE attempt_id = :a",
        a=attempt,
    )
    return row


async def _finish(pg: DatabaseSettings, attempt: str, phases: dict[str, int]) -> None:
    await _sql(
        pg,
        "UPDATE attempt SET state = 'done', ended_at = now() WHERE id = :a",
        a=attempt,
    )
    for phase, ms in phases.items():
        await _sql(
            pg,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, :s)",
            a=attempt,
            p=phase,
            ms=ms,
            s=ms == 0,
        )


async def test_the_calibration_read_sees_the_analytical_raw_not_the_ml_value(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    nodes = await _nodes(pg_settings, 2)
    planner = _planner(Manager(nodes))
    ml, _ = stub_ml(
        {
            ("compute", None): Rule(y_hat=math.log(0.25)),
            ("env", None): Rule(y_hat=math.log(2.0)),
        }
    )
    planner.ml = ml
    attempts = []
    for i in range(6):
        await _queue(store, f"{i + 1}" * 64)
        attempts.extend((await _tick(store, planner)).values())
    assert len(attempts) == 6
    for attempt in attempts:
        await _finish(
            pg_settings,
            attempt,
            {"stage": 0, "env": 10_000, "run": 30_000, "collect": 0},
        )
    engine = create_engine(pg_settings)
    try:
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as db:
            outcomes = await read_outcomes(db, 100)
    finally:
        await engine.dispose()
    assert len(outcomes) == 6
    runs = _match_all(outcomes)
    assert len(runs) == 6
    for run in runs:
        by_term = {o.term: o for o in run.terms}
        # the analytical defaults: 60 s compute, 20 s env - never 15 s / 40 s
        assert by_term["compute"].predicted_s == pytest.approx(60.0)
        assert by_term["env"].predicted_s == pytest.approx(20.0)
        assert by_term["compute"].actual_s == pytest.approx(30.0)
    calibration = fit(runs, CalibrationConfig())
    compute = calibration.terms["compute"].global_
    assert compute is not None
    assert compute.median == pytest.approx(0.5)  # 30 / 60, not 30 / 15
    env = calibration.terms["env"].global_
    assert env is not None and env.median == pytest.approx(0.5)  # 10 / 20


async def test_models_change_between_ticks_and_every_row_keeps_its_own(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _nodes(pg_settings, 2)
    planner = _planner(Manager([a, b]))
    ml_a, _ = stub_ml(
        {("compute", b): Rule(y_hat=math.log(0.25)), ("compute", None): Rule()}
    )
    ml_b, _ = stub_ml(
        {("compute", a): Rule(y_hat=math.log(0.25)), ("compute", None): Rule()},
        version="stub-v2",
    )
    seen: list[tuple[str, MlInput | None, Any]] = []
    for label, model in (("A", ml_a), ("none", None), ("B", ml_b), ("A2", ml_a)):
        planner.ml = model
        await _queue(store, "9" * 64)
        ((_job, attempt),) = (await _tick(store, planner)).items()
        seen.append((label, model, await _row(pg_settings, attempt)))
    versions = [
        None if "ml" not in r.replay else r.replay["ml"]["version"]
        for _l, _m, r in seen
    ]
    assert versions == ["stub-v1", None, "stub-v2", "stub-v1"]
    assert "ml_model_version" not in seen[1][2].candidates[0]  # a V0.14 row
    # the model moved each dispatch to its own favourite
    assert seen[0][2].chosen_node_id == b and seen[2][2].chosen_node_id == a
    models = {"stub-v1": ml_a, "stub-v2": ml_b}
    for _label, _model, row in seen:
        used = row.replay.get("ml")
        if used is None:
            again = replay_decision(row.replay, row.policy, load=lambda _v: None)
        else:
            again = replay_decision(row.replay, row.policy, load=models.get)
        assert again.status == "ok" and again.decision is not None
        assert candidates_json(again.decision) == row.candidates
        assert again.decision.snapshot_digest == row.snapshot_digest
        if used is not None:
            other = "stub-v2" if used["version"] == "stub-v1" else "stub-v1"
            wrong = replay_decision(
                row.replay, row.policy, load=lambda _v, o=other: models[o]
            )
            assert wrong.status == ML_MODEL_UNAVAILABLE


async def test_a_batch_in_one_tick_uses_the_tick_s_model_for_every_job(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _nodes(pg_settings, 2)
    planner = _planner(Manager([a, b]))
    ml, stub = stub_ml({("compute", None): Rule(y_hat=-0.1)})
    planner.ml = ml
    for i in range(8):
        await _queue(store, f"{i + 1}" * 64)
    dispatched = await _tick(store, planner)
    assert len(dispatched) == 8
    for attempt in dispatched.values():
        row = await _row(pg_settings, attempt)
        assert {c["ml_model_version"] for c in row.candidates} == {"stub-v1"}
        assert row.replay["ml"] == {"version": "stub-v1"}
    assert stub.calls == 8  # one predict call per Mode 1 decision


async def test_failing_decisions_are_counted_and_logged_once_a_minute(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _nodes(pg_settings, 2)
    clock = Clock()
    planner = _planner(Manager([a, b]), clock)
    ml, _ = stub_ml({("compute", None): Rule()}, raises=OSError("model file gone"))
    planner.ml = ml
    with structlog.testing.capture_logs() as logs:
        for i in range(3):
            await _queue(store, f"{i + 1}" * 64)
            assert len(await _tick(store, planner)) == 1  # still dispatches
        assert planner.ml_predictor_errors == 3
        first = [e for e in logs if e["event"] == "controller.jobs.ml_predictor_error"]
        assert len(first) == 1, logs
        assert first[0]["error_type"] == "OSError"
        clock.now += 59.0
        await _queue(store, "4" * 64)
        await _tick(store, planner)
        assert (
            len([e for e in logs if e["event"] == "controller.jobs.ml_predictor_error"])
            == 1
        )
        clock.now += 2.0  # 61 s after the first
        await _queue(store, "5" * 64)
        await _tick(store, planner)
        assert (
            len([e for e in logs if e["event"] == "controller.jobs.ml_predictor_error"])
            == 2
        )
    assert planner.ml_predictor_errors == 5


async def test_a_model_that_breaks_mid_stream_never_leaves_a_job_queued(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _nodes(pg_settings, 2)
    planner = _planner(Manager([a, b]))
    good, _ = stub_ml({("compute", None): Rule(y_hat=0.1)})
    bad, _ = stub_ml({("compute", None): Rule()}, answer_count_delta=1)
    for label, model in (("good", good), ("bad", bad), ("good", good), ("bad", bad)):
        planner.ml = model
        job = await _queue(store, "7" * 64)
        dispatched = await _tick(store, planner)
        assert job in dispatched, label
        row = await _row(pg_settings, dispatched[job])
        if model is bad:
            assert row.replay["ml"]["fallback"] == "malformed_prediction"
            assert all(c["ml_fallback"] == "predictor_error" for c in row.candidates)
        else:
            assert "fallback" not in row.replay["ml"]
    assert planner.ml_predictor_errors == 2
