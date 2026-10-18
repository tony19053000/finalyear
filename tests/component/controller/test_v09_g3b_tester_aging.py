"""V0.9 G3 lane B tester r1/r2: D215 says a benchmark job is the lowest class and
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
from test_v06_g4_criterion4 import _sql
from test_v09_g3_fix1_aging import _job as _keyed_job
from test_v09_g3_fix1_aging import _link

from meshforge.common.manifest import MIN_PRIORITY
from meshforge.controller.bench.submit import IDEMPOTENCY_PREFIX
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.queue_reads import queued_jobs
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


async def _order(pg: DatabaseSettings, now: dt.datetime) -> list[str]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            jobs = await queued_jobs(db, limit=10, now=now, aging_s=AGING_S)
    finally:
        await engine.dispose()
    return [j.id for j in jobs]


async def _old(pg: DatabaseSettings, job_id: str, periods: int) -> dt.datetime:
    now = dt.datetime.now(dt.UTC)
    await _sql(
        pg,
        "UPDATE job SET queued_at = :t, created_at = :t WHERE id = :j",
        t=now - dt.timedelta(seconds=AGING_S * periods),
        j=job_id,
    )
    return now


@pytest.mark.parametrize("waited_periods", [11, 25, 1000])
@pytest.mark.parametrize("identity", ["bench_run-link", "key-only-window"])
async def test_a_long_waiting_bench_job_never_outranks_a_fresh_user_job(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    waited_periods: int,
    identity: str,
) -> None:
    """r2 (D237): a real benchmark job - linked from bench_run, or only carrying
    the ``bench:`` Idempotency-Key (submitted, link not yet committed)."""
    key = f"{IDEMPOTENCY_PREFIX}brun_r2" if identity == "key-only-window" else None
    bench = await _keyed_job(store, 1, MIN_PRIORITY, key)
    if identity == "bench_run-link":
        await _link(pg_settings, bench.id)
    user = await _keyed_job(store, 2, 0)
    now = await _old(pg_settings, bench.id, waited_periods)
    order = await _order(pg_settings, now)
    assert order.index(user.id) < order.index(bench.id), (
        "D215 violated: a bench job aged past a fresh user job",
        order,
    )


async def test_a_plain_user_minus_ten_job_still_ages_d106(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    low = await _keyed_job(store, 1, MIN_PRIORITY, "not-bench:x")  # prefix mid-key
    user = await _keyed_job(store, 2, 0)
    now = await _old(pg_settings, low.id, 25)
    order = await _order(pg_settings, now)
    assert order.index(low.id) < order.index(user.id), order


@pytest.mark.parametrize("key", ["BENCH:x", "bench", "bench-x", " bench:x", "xbench:x"])
async def test_near_miss_keys_are_not_benchmark_identity(
    pg_settings: DatabaseSettings, store: ExecutionStore, key: str
) -> None:
    low = await _keyed_job(store, 1, MIN_PRIORITY, key)
    user = await _keyed_job(store, 2, 0)
    now = await _old(pg_settings, low.id, 25)
    order = await _order(pg_settings, now)
    assert order.index(low.id) < order.index(user.id), (key, order)


@pytest.mark.parametrize("priority", [-10, -3, 0, 5, 9, 10])
async def test_forging_a_bench_key_can_never_raise_priority(
    pg_settings: DatabaseSettings, store: ExecutionStore, priority: int
) -> None:
    """A user forging ``bench:`` only loses aging; it never ranks above the same
    job without the key, nor above an unaged job of a higher own priority."""
    forged = await _keyed_job(store, 1, priority, f"{IDEMPOTENCY_PREFIX}forged")
    honest = await _keyed_job(store, 2, priority, "honest")
    now = await _old(pg_settings, forged.id, 40)
    await _sql(
        pg_settings,
        "UPDATE job SET queued_at = :t, created_at = :t WHERE id = :j",
        t=now - dt.timedelta(seconds=AGING_S * 40 + 5),  # waited longer: wins ties
        j=honest.id,
    )
    order = await _order(pg_settings, now)
    assert order.index(honest.id) <= order.index(forged.id), order
    if priority < 10:
        higher = await _keyed_job(store, 3, priority + 1, None)
        order = await _order(pg_settings, dt.datetime.now(dt.UTC))
        assert order.index(higher.id) < order.index(forged.id), order
