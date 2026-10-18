"""V0.9 G3 lane B item 2: the campaign store on real PostgreSQL (migration 0022)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.bench.campaign import NodeRef, expand, parse_spec, spec_digest
from meshforge.common.ids import new_campaign_id, new_job_id
from meshforge.controller.bench.store import BenchStore, campaign_identities
from meshforge.controller.jobs.store import JobStore
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

SPEC: dict[str, Any] = {
    "name": "store-test",
    "seed": 3,
    "families": [{"family": "cpu.sort"}, {"family": "cpu.matmul"}],
    "strategies": ["local", "mode1"],
    "origin": "node_a",
    "budget": {"max_runs": 100},
}
NODES = [NodeRef("node_a"), NodeRef("node_b")]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _create(store: BenchStore) -> str:
    spec = parse_spec(SPEC)
    expansion = expand(spec, NODES)
    campaign_id = new_campaign_id()
    record = await store.create_campaign(
        campaign_id=campaign_id,
        name=spec.name,
        spec_yaml="name: store-test\n",
        spec_digest=spec_digest(spec),
        seed=spec.seed,
        expansion=expansion,
        nodes=[n.node_id for n in NODES],
        created_by=None,
    )
    assert record.run_count == 12 and record.counts["planned"] == 12
    assert record.summary["configs"] == 4 and record.summary["nodes"] == [
        "node_a",
        "node_b",
    ]
    return campaign_id


async def _job(jobs: JobStore) -> str:
    job_id = str(new_job_id())
    await jobs.create_queued_job(
        job_id=job_id,
        name="b",
        manifest={"name": "b", "entrypoint": ["python", "x.py"]},
        bundle_digest="sha256:" + "0" * 64,
        placement={"mode": "offload", "node_id": "node_b"},
        request_hash="h",
        idempotency_key=None,
    )
    return job_id


async def test_create_list_and_runs_in_seq_order(engine: AsyncEngine) -> None:
    store = BenchStore(engine)
    campaign_id = await _create(store)
    runs = await store.runs(campaign_id, limit=5)
    assert [r.seq for r in runs] == [1, 2, 3, 4, 5]
    rest = await store.runs(campaign_id, after_seq=5, limit=100)
    assert [r.seq for r in rest] == list(range(6, 13))
    planned = expand(parse_spec(SPEC), NODES).runs
    assert [(r.config_id, r.repetition, r.seed) for r in runs + rest] == [
        (p.config_id, p.repetition, p.seed) for p in planned
    ]
    listed = await store.list_campaigns()
    assert [c.id for c in listed] == [campaign_id]
    assert await store.get_campaign("camp_nope") is None


async def test_submit_skip_sync_and_identities(engine: AsyncEngine) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _create(store)
    runs = await store.runs(campaign_id, limit=100)
    first, second = runs[0], runs[1]
    job_one = await _job(jobs)
    assert await store.mark_submitted(first.id, job_one)
    assert not await store.mark_submitted(first.id, await _job(jobs))  # CAS lost
    assert await store.mark_skipped(second.id, "node gone")
    assert not await store.mark_skipped(second.id, "again")

    async with create_session_factory(engine)() as db:
        found = await campaign_identities(db, [job_one, "job_other"])
    assert found == {
        job_one: {
            "campaign_id": campaign_id,
            "group_id": first.group_id,
            "config_id": first.config_id,
            "repetition": first.repetition,
            "run_id": first.id,
        }
    }
    assert await store.sync(campaign_id) == 0  # job still queued
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE job SET state = 'failed', failure_type = 'node_lost' WHERE id = :j"
            ),
            {"j": job_one},
        )
    assert await store.sync(campaign_id) == 1
    campaign = await store.get_campaign(campaign_id)
    assert campaign is not None
    assert campaign.counts == {
        "planned": 10,
        "submitted": 0,
        "done": 0,
        "failed": 1,
        "skipped": 1,
    }
    assert campaign.state == "active"
    row = (await store.runs(campaign_id, limit=1))[0]
    assert row.state == "failed" and row.detail == "job failed: node_lost"
    assert row.ended_at is not None


async def test_campaign_is_done_when_no_run_is_open(engine: AsyncEngine) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _create(store)
    runs = await store.runs(campaign_id, limit=100)
    for run in runs[:-1]:
        await store.mark_skipped(run.id, "test")
    job_id = await _job(jobs)
    await store.mark_submitted(runs[-1].id, job_id)
    await store.sync(campaign_id)
    assert (await store.get_campaign(campaign_id)).state == "active"  # type: ignore[union-attr]
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE job SET state = 'succeeded' WHERE id = :j"), {"j": job_id}
        )
    assert await store.sync() == 1  # every campaign
    done = await store.get_campaign(campaign_id)
    assert done is not None and done.state == "done" and done.counts["done"] == 1


async def test_large_campaign_inserts_in_one_transaction(engine: AsyncEngine) -> None:
    store = BenchStore(engine)
    spec = parse_spec(
        {
            **SPEC,
            "families": [
                {
                    "family": "cpu.matmul",
                    "params": {
                        "repeats": {"min": 22, "max": 200, "int": True, "steps": 60}
                    },
                }
            ],
            "repetitions": 20,
            "budget": {"max_runs": 5000},
        }
    )
    expansion = expand(spec, NODES)
    assert len(expansion.runs) > 2000
    record = await store.create_campaign(
        campaign_id=new_campaign_id(),
        name=spec.name,
        spec_yaml="x",
        spec_digest=spec_digest(spec),
        seed=1,
        expansion=expansion,
        nodes=["node_a", "node_b"],
        created_by=None,
    )
    fetched = await store.get_campaign(record.id)
    assert fetched is not None and fetched.counts["planned"] == len(expansion.runs)
