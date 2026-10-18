"""V0.9 G3 lane B fix r1 (D-1, D235): benchmark jobs never age in the queue -
a fresh user job is always ahead of them - while a user's own -10 job still
ages (V0.6 D106 unchanged)."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy.ext.asyncio import AsyncSession
from test_v06_g4_criterion4 import MB, _digest, _sql

from meshforge.common.ids import new_job_id
from meshforge.common.manifest import MIN_PRIORITY
from meshforge.controller.bench.submit import IDEMPOTENCY_PREFIX
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.queue_reads import queued_jobs
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

AGING_S = 600.0
MANIFEST = {"name": "a", "entrypoint": ["python", "m.py"]}


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _job(
    store: ExecutionStore, n: int, priority: int, key: str | None = None
) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="a",
        manifest=MANIFEST,
        bundle_digest=_digest(n),
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=key,
        scheduling=JobScheduling(
            priority=priority, fingerprint=str(n) * 64, input_bytes=MB
        ),
    )
    return job


async def _age(pg: DatabaseSettings, job_id: str, periods: int) -> None:
    old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=AGING_S * periods)
    await _sql(
        pg,
        "UPDATE job SET queued_at = :t, created_at = :t WHERE id = :j",
        t=old,
        j=job_id,
    )


async def _link(pg: DatabaseSettings, job_id: str) -> None:
    await _sql(
        pg,
        "INSERT INTO bench_campaign (id, name, spec_yaml, spec_digest, seed, state, "
        "run_count, summary) VALUES ('camp_x', 'c', 'x', 'sha256:x', 1, 'active', 1, "
        "'{}'::jsonb) ON CONFLICT DO NOTHING",
    )
    await _sql(
        pg,
        "INSERT INTO bench_run (id, campaign_id, seq, group_id, config_id, repetition, "
        "family, params, seed, conditions, strategy, node_id, origin_node_id, state, "
        "job_id) VALUES (:r, 'camp_x', 1, 'g', 'c', 1, 'cpu.sort', '{}'::jsonb, 1, "
        "'[]'::jsonb, 'local', 'n', 'n', 'submitted', :j)",
        r=f"brun_{job_id}",
        j=job_id,
    )


async def _order(pg: DatabaseSettings) -> list[str]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            jobs = await queued_jobs(
                db, limit=10, now=dt.datetime.now(dt.UTC), aging_s=AGING_S
            )
    finally:
        await engine.dispose()
    return [j.id for j in jobs]


@pytest.mark.parametrize("identity", ["linked", "keyed"])
async def test_a_benchmark_job_never_ages_past_a_fresh_user_job(
    pg_settings: DatabaseSettings, store: ExecutionStore, identity: str
) -> None:
    key = f"{IDEMPOTENCY_PREFIX}brun_x" if identity == "keyed" else None
    bench = await _job(store, 1, MIN_PRIORITY, key)
    if identity == "linked":
        await _link(pg_settings, bench.id)
    await _age(pg_settings, bench.id, 1000)
    user = await _job(store, 2, 0)
    order = await _order(pg_settings)
    assert order.index(user.id) < order.index(bench.id), order


async def test_a_users_own_lowest_priority_job_still_ages(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    low = await _job(store, 1, MIN_PRIORITY, "a-user-key")
    await _age(pg_settings, low.id, 11)
    user = await _job(store, 2, 0)
    order = await _order(pg_settings)
    assert order.index(low.id) < order.index(user.id), order  # D106 unchanged
