"""V0.14 G6 (D324) on a real PostgreSQL: stored decision latency.

* migration 0033: ``plan_decision.decision_ms`` (REAL, NULL) and its check
  (never negative) up from 0032; down drops both; up again twice; ``alembic
  check`` is clean (model == migration);
* a real dispatch tick (:class:`DispatchPlanner` -> ``dispatch_planned``)
  writes ``decision_ms`` on every ``dispatched`` row; the decisions read and
  the API answer return it;
* the evaluator's latency read takes exactly the campaign's dispatched
  decisions (not another job's, not a ``waiting`` row), sizes each by its
  replay's node count, and a NULL row (pre-0033) is excluded and counted;
  the stored report's ``latency`` block and criterion 3 come from them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

from alembic import command
from mf_eval_pg import (
    DIGEST,
    MANIFEST,
    dispatch_runs,
    finish_runs,
    make_campaign,
    medians,
    planner,
    register_nodes,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meshforge.common.ids import new_job_id
from meshforge.controller.api.decision_routes import _out
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.latency import LatencySample
from meshforge.controller.planner_eval.loader import load_latency
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]

CHECK = "ck_plan_decision_decision_ms"


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _q(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return list(res.all()) if res.returns_rows else []
    finally:
        await engine.dispose()


def _column(settings: DatabaseSettings) -> list[Any]:
    return _run(
        _q(
            settings,
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'plan_decision' AND column_name = 'decision_ms'",
        )
    )


def _has_check(settings: DatabaseSettings) -> bool:
    return bool(
        _run(
            _q(
                settings,
                "SELECT 1 FROM pg_constraint WHERE conname = :n AND contype = 'c'",
                n=CHECK,
            )
        )
    )


def test_0033_up_check_down_up_and_alembic_check(pg_settings: DatabaseSettings) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0032")
    assert _column(pg_settings) == [] and not _has_check(pg_settings)
    command.upgrade(cfg, "0033")
    assert [tuple(r) for r in _column(pg_settings)] == [("real", "YES")]
    assert _has_check(pg_settings)
    # the check refuses a negative time (a job row is needed for the FK)
    job = str(new_job_id())
    _run(
        _q(
            pg_settings,
            "INSERT INTO job (id, name, state, manifest, bundle_digest, placement, "
            "request_hash, attempt_epoch, created_at, updated_at) VALUES (:j, 'x', "
            "'queued', '{}'::jsonb, :d, '{}'::jsonb, :h, 0, now(), now())",
            d="sha256:" + "a" * 64,
            j=job,
            h="h" * 64,
        )
    )
    insert = (
        "INSERT INTO plan_decision (job_id, attempt_no, outcome, policy, "
        "snapshot_digest, reason_key, candidates, decision_ms) VALUES (:j, 1, "
        "'waiting', 'cost_model', 'd', 'r', '[]'::jsonb, :ms)"
    )
    for ok in (None, 0.0, 12.5):
        _run(_q(pg_settings, insert, j=job, ms=ok))
    with pytest.raises(DBAPIError) as caught:
        _run(_q(pg_settings, insert, j=job, ms=-0.5))
    assert "violates" in str(caught.value)
    for _ in range(2):
        command.downgrade(cfg, "0032")
        assert _column(pg_settings) == [] and not _has_check(pg_settings)
        command.upgrade(cfg, "head")
        assert _has_check(pg_settings)
    command.check(cfg)


# ---------------------------------------------------------------- real dispatch


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _rows(engine: Any, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


@pytest.mark.asyncio
async def test_a_real_dispatch_stores_decision_ms_and_the_evaluator_reads_it(
    engine: Any,
) -> None:
    nodes = await register_nodes(engine, 3)
    campaign_id, runs = await make_campaign(engine, nodes, [20, 22])
    store = ExecutionStore(engine)
    dispatcher = planner(nodes)
    dispatched = await dispatch_runs(store, dispatcher, runs)
    assert len(dispatched) == 18

    # every dispatched row of the campaign carries its time
    rows = await _rows(
        engine,
        "SELECT job_id, decision_ms FROM plan_decision WHERE outcome = 'dispatched'",
    )
    assert len(rows) == 18
    assert all(r[1] is not None and r[1] >= 0.0 for r in rows), rows
    # ... and the decisions read + the API answer return it
    job_id = dispatched[0].job_id
    (record,) = await store.decisions(job_id)
    stored = next(r[1] for r in rows if r[0] == job_id)
    assert record.decision_ms == round(float(stored), 3)
    assert _out(record).decision_ms == record.decision_ms

    # a user's job (not the campaign's) and a NULL (pre-0033) row
    other = str(new_job_id())
    await store.create_queued_job(
        job_id=other,
        name="user",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(input_bytes=0, submitter_node_id=nodes[0]),
    )
    await store.dispatch_planned(
        dispatcher.decide,
        limit=10,
        max_per_node=500,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )
    assert (
        await _rows(
            engine, "SELECT count(*) FROM plan_decision WHERE job_id = :j", j=other
        )
    )[0][0] == 1
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE plan_decision SET decision_ms = NULL WHERE job_id = :j"),
            {"j": dispatched[1].job_id},
        )
        # a campaign job's earlier `waiting` row: a decision, not a dispatch
        await conn.execute(
            text(
                "INSERT INTO plan_decision (job_id, attempt_no, outcome, policy, "
                "snapshot_digest, reason_key, candidates, decision_ms) VALUES "
                "(:j, 1, 'waiting', 'cost_model', 'd', 'r', '[]'::jsonb, 5.0)"
            ),
            {"j": dispatched[2].job_id},
        )

    samples = await load_latency(engine, [campaign_id])
    assert len(samples) == 18  # the campaign's dispatched rows, not the user's
    assert all(s.node_count == 3 for s in samples)  # the replay's snapshot
    assert sum(1 for s in samples if s.decision_ms is None) == 1
    assert await load_latency(engine, []) == []

    await finish_runs(
        engine,
        dispatched,
        medians({nodes[0]: 900, nodes[1]: 600, nodes[2]: 300}),
    )
    service = EvalService(
        engine,
        CalibrationStore(engine),
        EvalReportStore(engine),
        CalibrationConfig(window_runs=200, shrink_k=5.0, clamp=(0.2, 5.0)),
        max_rows=10_000,
    )
    row = await service.run([campaign_id])
    latency = row.report["latency"]
    assert latency["decisions"] == 17 and latency["without_latency"] == 1
    assert list(latency["by_node_count"]) == ["3"]
    assert latency["by_node_count"]["3"]["decisions"] == 17
    timed = sorted(
        float(r[1])
        for r in await _rows(
            engine,
            "SELECT job_id, decision_ms FROM plan_decision WHERE outcome = "
            "'dispatched' AND decision_ms IS NOT NULL AND job_id <> :o",
            o=other,
        )
    )
    assert latency["max_ms"] == round(timed[-1], 6)
    assert latency["p99_ms"] == round(timed[-1], 6)  # 17 values: rank 17
    (c3,) = [v for v in row.report["verdicts"] if v["criterion"] == 3]
    assert c3["status"] == "too_few_decisions"  # 3 nodes, the criterion wants 4
    assert c3["counted_groups"] == 0
    assert isinstance(samples[0], LatencySample)
