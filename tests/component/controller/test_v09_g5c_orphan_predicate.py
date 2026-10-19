"""V0.9 G5c item 0.7 (D252.4) on real PostgreSQL: one "this job is that run's"
predicate (:func:`~meshforge.controller.bench.store.runs_own_job`) for linking
and for the orphan backstop - tested both ways.

* the run's own job (owner, origin, pin all match) is **linked** by ``sync``
  and, once its campaign is cancelled, **is** an orphan;
* a look-alike under the same ``bench:<run_id>`` key (another node, another
  origin, another owner) is **neither** linked **nor** reported as an orphan
  (before G5c the backstop checked only the owner - a job pinned elsewhere or
  submitted from elsewhere would have been cancelled as the run's).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from mf_users import create_user
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g4a_bench_store_sync import (  # type: ignore[import-not-found]
    _campaign,
    _job,
    _run,
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


async def test_the_runs_own_job_is_linked_and_then_an_orphan(
    engine: AsyncEngine,
) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _campaign(store)
    run = (await store.runs(campaign_id, limit=1))[0]
    job_id = await _job(jobs, run)
    await store.sync(campaign_id)
    assert (await _run(store, campaign_id, run.id)).job_id == job_id  # linked
    assert await store.orphan_jobs(campaign_id) == []  # its run is live
    assert await CampaignControl(engine).cancel(campaign_id) == [job_id]
    assert await store.orphan_jobs(campaign_id) == [job_id]  # now an orphan


@pytest.mark.parametrize("wrong", ["node", "origin", "owner"])
async def test_a_look_alike_is_neither_linked_nor_an_orphan(
    engine: AsyncEngine, wrong: str
) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _campaign(store)
    run = (await store.runs(campaign_id, limit=1))[0]
    other_node = "node_b" if run.node_id == "node_a" else "node_a"
    mallory = await create_user(engine, "mallory", "mallory-password-1")
    await _job(
        jobs,
        run,
        node_id=other_node if wrong == "node" else None,
        origin="node_zz" if wrong == "origin" else None,
        owner=mallory if wrong == "owner" else None,
    )
    await store.sync(campaign_id)
    kept = await _run(store, campaign_id, run.id)
    assert kept.state == "planned" and kept.job_id is None  # not linked
    await CampaignControl(engine).cancel(campaign_id)  # the run is skipped now
    assert (await _run(store, campaign_id, run.id)).state == "skipped"
    assert await store.orphan_jobs(campaign_id) == []  # and not cancelled
