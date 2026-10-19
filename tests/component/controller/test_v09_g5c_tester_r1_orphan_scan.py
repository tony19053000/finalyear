"""V0.9 G5c tester r1 (item 0.7): an orphan born through a path that never
calls ``expect_orphans()`` (here: the control's cancel, a different object from
the store) is still found by the backstop within ``orphan_scan_s`` - and at
once with ``orphan_scan_s = 0``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g4a_bench_store_sync import (  # type: ignore[import-not-found]
    _campaign,
    _job,
)

from meshforge.controller.bench.control import CampaignControl
from meshforge.controller.bench.store import BenchStore
from meshforge.controller.jobs.store import JobStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _scenario(
    engine: AsyncEngine, scan_s: float
) -> tuple[list[str], str, BenchStore]:
    store, jobs = BenchStore(engine), JobStore(engine)
    asked: list[str] = []

    async def cancel(job_id: str) -> None:
        asked.append(job_id)

    store.cancel_job = cancel
    store.orphan_scan_s = scan_s
    campaign_id = await _campaign(store)
    run = (await store.runs(campaign_id, limit=1))[0]
    job_id = await _job(jobs, run)
    await store.sync()  # first whole scan: clean; links the job
    assert asked == []
    # an orphan appears without anybody telling the store
    assert await CampaignControl(engine).cancel(campaign_id) == [job_id]
    return asked, job_id, store


async def test_an_unannounced_orphan_is_found_within_the_scan_interval(
    engine: AsyncEngine,
) -> None:
    asked, job_id, store = await _scenario(engine, 1.0)
    await store.sync()
    assert asked == []  # throttled: within the interval
    await asyncio.sleep(1.2)
    await store.sync()
    assert asked == [job_id]


async def test_scan_every_sync_when_the_interval_is_zero(engine: AsyncEngine) -> None:
    asked, job_id, store = await _scenario(engine, 0.0)
    await store.sync()
    assert asked == [job_id]
