"""V0.14 G5 (D300, D320) on a real PostgreSQL: the decisions answer's new
fields, from rows a real dispatch tick wrote.

* a ``done`` attempt: per-term ``actual_s`` / ``ratio`` on the chosen
  candidate exactly where ``calibration.match`` pairs a phase (``stage`` ->
  transfer, ``run`` -> compute, ``collect`` -> output; a skipped ``env`` -> null),
  ``replay_version`` set (every V0.14 decision has a replay), no calibration
  yet -> ``calibration_version`` null and no term ``calibration``;
* a calibrated tick: ``calibration_version`` and the term's factor block;
* a ``failed`` attempt and one still running: no per-term actual at all;
* the store never loads the replay blob for this read (deferred column).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command

# the G2 component suite's real-dispatch helpers (one node, assumed 60 s compute)
from test_v014_g2_calibration_pg import (  # type: ignore[import-not-found]
    DEFAULT_COMPUTE_S,
    DIGEST,
    MANIFEST,
    Manager,
    _node,
    _planner,
    _seed,
    _sql,
)

from meshforge.common.ids import new_job_id
from meshforge.controller.api.decision_routes import DecisionOut, _out
from meshforge.controller.calibration_store import CalibrationSource, CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

CFG = CalibrationConfig(window_runs=200, shrink_k=5.0, clamp=(0.2, 5.0))


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _job(
    store: ExecutionStore, planner: DispatchPlanner, fp: str
) -> tuple[str, str]:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g5",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint=fp, input_bytes=0),
    )
    results = await store.dispatch_planned(
        planner.decide,
        limit=10,
        max_per_node=64,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )
    (result,) = [r for r in results if r.job.id == job.id]
    assert result.attempt is not None
    return job.id, result.attempt.id


async def _end(
    pg: DatabaseSettings, attempt_id: str, state: str, phases: dict[str, int | None]
) -> None:
    await _sql(
        pg,
        "UPDATE attempt SET state = :s, ended_at = now(), failure_type = :f WHERE id = :a",
        a=attempt_id,
        s=state,
        f="node_lost" if state == "failed" else None,
    )
    for phase, ms in phases.items():
        await _sql(
            pg,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, :s)",
            a=attempt_id,
            p=phase,
            ms=0 if ms is None else ms,
            s=ms is None,
        )


async def _answer(store: ExecutionStore, job_id: str) -> DecisionOut:
    (record,) = await store.decisions(job_id)
    return _out(record)


def _chosen_terms(out: DecisionOut) -> dict[str, Any]:
    (chosen,) = [c for c in out.candidates if c.rank == 1]
    assert chosen.estimate is not None
    return dict(chosen.estimate.terms)


PHASES = {"queue": 30, "stage": 2_000, "env": None, "run": 90_000, "collect": 500}


async def test_a_done_attempt_answers_actuals_per_term_from_the_match(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    job_id, attempt = await _job(store, planner, "a" * 64)
    running = await _answer(store, job_id)
    assert running.actual is not None and running.actual.state != "done"
    assert all(t.actual_s is None for t in _chosen_terms(running).values())

    await _end(pg_settings, attempt, "done", PHASES)
    out = await _answer(store, job_id)
    assert out.replay_version is not None and out.calibration_version is None
    assert out.margin_sentence is None  # a whole job
    terms = _chosen_terms(out)
    assert terms["compute"].seconds == DEFAULT_COMPUTE_S
    assert terms["compute"].actual_s == 90.0
    assert terms["compute"].ratio == pytest.approx(90.0 / DEFAULT_COMPUTE_S)
    assert terms["output"].actual_s == 0.5
    assert terms["transfer"].actual_s == 2.0
    assert terms["env"].actual_s is None  # skipped: no honest phase
    assert terms["queue"].actual_s is None  # never a calibrated term
    assert all(t.calibration is None for t in terms.values())
    # the other candidates never ran
    for c in out.candidates:
        if c.rank != 1 and c.estimate is not None:
            assert all(t.actual_s is None for t in c.estimate.terms.values())


async def test_a_failed_attempt_has_no_per_term_actual(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    node = await _node(pg_settings)
    job_id, attempt = await _job(store, _planner(Manager([node])), "b" * 64)
    await _end(pg_settings, attempt, "failed", PHASES)
    out = await _answer(store, job_id)
    assert out.actual is not None and out.actual.failure_type == "node_lost"
    assert out.actual.phases_ms["run"] == 90_000  # the raw phases are still shown
    assert all(
        t.actual_s is None and t.ratio is None for t in _chosen_terms(out).values()
    )


async def test_a_calibrated_decision_names_its_version_and_factor(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    await _seed(pg_settings, store, planner, [120.0] * 10)
    engine = create_engine(pg_settings, pool_size=3)
    try:
        calibrations = CalibrationStore(engine)
        fitted = await calibrations.refresh(CFG)
        assert fitted.row is not None and fitted.row.status == "active"
        planner.calibration = await CalibrationSource(calibrations, "auto").refresh()
    finally:
        await engine.dispose()
    job_id, _attempt = await _job(store, planner, "c" * 64)
    out = await _answer(store, job_id)
    assert out.calibration_version == fitted.row.version
    compute = _chosen_terms(out)["compute"]
    assert compute.calibration is not None
    assert compute.calibration.version == fitted.row.version
    assert compute.calibration.raw_s == DEFAULT_COMPUTE_S
    assert compute.calibration.key_level == "node" and compute.calibration.n == 8
    assert compute.seconds == pytest.approx(
        DEFAULT_COMPUTE_S * compute.calibration.factor
    )


async def test_the_read_never_loads_the_replay_blob(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    node = await _node(pg_settings)
    job_id, _attempt = await _job(store, _planner(Manager([node])), "d" * 64)
    (row,) = await _sql(
        pg_settings,
        "SELECT replay IS NOT NULL AS has, replay_version FROM plan_decision "
        "WHERE job_id = :j",
        j=job_id,
    )
    assert row.has and row.replay_version is not None
    statements: list[str] = []
    engine = create_engine(pg_settings, pool_size=1)
    try:
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _seen(_conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
            statements.append(statement)

        (record,) = await ExecutionStore(engine).decisions(job_id)
    finally:
        await engine.dispose()
    assert record.replay_version == row.replay_version
    decision_reads = [s for s in statements if "FROM plan_decision" in s]
    assert decision_reads
    # ``plan_decision.replay`` as a column of its own (``replay_version`` is
    # one, and is read)
    loaded = re.compile(r"plan_decision\.replay\b")
    assert not any(loaded.search(s) for s in decision_reads), decision_reads
    assert any("plan_decision.replay_version" in s for s in decision_reads)
