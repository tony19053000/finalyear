"""V0.9 G3 lane B tester r1: D215 says a benchmark job is the lowest class and
is *never ahead of user jobs*. Priority aging lifts every queued job by one level
per ``priority_aging_s`` up to the top level, so a bench job that waited long
enough outranks a freshly queued user job. This is the Lead-ruled defect."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy.ext.asyncio import AsyncSession
from test_v06_g4_criterion4 import MB, _digest, _job, _sql

from meshforge.common.manifest import MIN_PRIORITY
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.queue_reads import queued_jobs
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

AGING_S = 600.0


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("waited_periods", [11, 25, 1000])
async def test_a_long_waiting_bench_job_never_outranks_a_fresh_user_job(
    pg_settings: DatabaseSettings, store: ExecutionStore, waited_periods: int
) -> None:
    now = dt.datetime.now(dt.UTC)
    bench = await _job(
        store,
        digest=_digest(1),
        scheduling=JobScheduling(
            priority=MIN_PRIORITY, fingerprint="1" * 64, input_bytes=MB
        ),
    )
    user = await _job(
        store,
        digest=_digest(2),
        scheduling=JobScheduling(priority=0, fingerprint="2" * 64, input_bytes=MB),
    )
    old = now - dt.timedelta(seconds=AGING_S * waited_periods)
    await _sql(
        pg_settings,
        "UPDATE job SET queued_at = :t, created_at = :t WHERE id = :j",
        t=old,
        j=bench.id,
    )
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            jobs = await queued_jobs(db, limit=10, now=now, aging_s=AGING_S)
    finally:
        await engine.dispose()
    order = [j.id for j in jobs]
    assert order.index(user.id) < order.index(bench.id), (
        "D215 violated: a bench job aged past a fresh user job",
        order,
    )
