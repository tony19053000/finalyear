"""V0.9 G4 lane A items 0.6 + 0.7 + 0.5 (store side) on real PostgreSQL.

* 0.6 (D239.2): ``BenchStore.sync`` links a ``planned`` run whose job exists
  under its ``bench:<run_id>`` key - and only a job that **is** that run's
  (owner = the campaign's creator, pinned to the run's node, submitted from its
  origin), never a look-alike;
* 0.7 (D239.3): a run's ``ended_at`` is the job's **final transition** time,
  not ``Job.updated_at`` (which moves when a terminal job is touched later);
* 0.5: ``runs(exclude_nodes=...)`` pages past the passed-over nodes in SQL.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from mf_users import create_user
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.bench.campaign import NodeRef, expand, parse_spec, spec_digest
from meshforge.common.ids import new_campaign_id, new_job_id
from meshforge.controller.bench.store import BENCH_KEY_PREFIX, BenchStore, RunRow
from meshforge.controller.bench.submit import IDEMPOTENCY_PREFIX
from meshforge.controller.jobs.queue_reads import BENCH_IDEMPOTENCY_PREFIX
from meshforge.controller.jobs.store import JobScheduling, JobStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

SPEC: dict[str, Any] = {
    "name": "sync-test",
    "seed": 3,
    "families": [{"family": "cpu.sort"}],
    "strategies": ["local", "mode1"],
    "origin": "node_a",
    "budget": {"max_runs": 100},
}
NODES = [NodeRef("node_a"), NodeRef("node_b")]
T_END = dt.datetime(2026, 10, 18, 10, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _campaign(store: BenchStore, created_by: str | None = None) -> str:
    spec = parse_spec(SPEC)
    campaign_id = new_campaign_id()
    await store.create_campaign(
        campaign_id=campaign_id,
        name=spec.name,
        spec_yaml="name: sync-test\n",
        spec_digest=spec_digest(spec),
        seed=spec.seed,
        expansion=expand(spec, NODES),
        nodes=[n.node_id for n in NODES],
        created_by=created_by,
    )
    return campaign_id


async def _job(
    jobs: JobStore,
    run: RunRow,
    *,
    key: str | None = None,
    node_id: str | None = None,
    origin: str | None = None,
    owner: str | None = None,
) -> str:
    """The job the submitter would have written for ``run`` (overrides make a
    look-alike)."""
    job_id = str(new_job_id())
    await jobs.create_queued_job(
        job_id=job_id,
        name="bench",
        manifest={"name": "bench", "entrypoint": ["python", "x.py"]},
        bundle_digest="sha256:" + "0" * 64,
        placement={"mode": "offload", "node_id": node_id or run.node_id},
        request_hash="h" * 64,
        idempotency_key=key or f"{IDEMPOTENCY_PREFIX}{run.id}",
        scheduling=JobScheduling(submitter_node_id=origin or run.origin_node_id),
        owner_user_id=owner,
    )
    return job_id


async def _run(store: BenchStore, campaign_id: str, run_id: str) -> RunRow:
    found = [r for r in await store.runs(campaign_id, limit=100) if r.id == run_id]
    assert len(found) == 1
    return found[0]


async def test_one_prefix_everywhere() -> None:
    assert IDEMPOTENCY_PREFIX == BENCH_KEY_PREFIX == BENCH_IDEMPOTENCY_PREFIX


async def test_sync_links_a_planned_run_to_its_committed_job(
    engine: AsyncEngine,
) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _campaign(store)
    run = (await store.runs(campaign_id, limit=1))[0]
    job_id = await _job(jobs, run)  # committed; the crash came before the link
    assert (await _run(store, campaign_id, run.id)).state == "planned"

    assert await store.sync(campaign_id) == 0  # linked, not ended
    linked = await _run(store, campaign_id, run.id)
    assert linked.state == "submitted" and linked.job_id == job_id
    assert linked.submitted_at is not None
    campaign = await store.get_campaign(campaign_id)
    assert campaign is not None and campaign.counts["submitted"] == 1
    assert not await store.mark_submitted(run.id, job_id)  # a later submit: CAS lost
    assert await store.sync(campaign_id) == 0  # idempotent


@pytest.mark.parametrize("wrong", ["node", "origin", "owner"])
async def test_a_look_alike_job_is_never_linked(
    engine: AsyncEngine, wrong: str
) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _campaign(store)
    run = (await store.runs(campaign_id, limit=1))[0]
    other_node = "node_b" if run.node_id == "node_a" else "node_a"
    # an owner that is not the campaign's creator (None = the legacy token)
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
    assert kept.state == "planned" and kept.job_id is None


async def test_ended_at_is_the_final_transition_not_updated_at(
    engine: AsyncEngine,
) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _campaign(store)
    first, second = (await store.runs(campaign_id, limit=2))[:2]
    job_one = await _job(jobs, first)
    job_two = await _job(jobs, second)
    assert await store.mark_submitted(first.id, job_one)
    assert await store.mark_submitted(second.id, job_two)
    async with engine.begin() as conn:
        for job_id, state in ((job_one, "succeeded"), (job_two, "failed")):
            await conn.execute(
                text(
                    "INSERT INTO job_transition (job_id, seq, from_state, to_state, at) "
                    "SELECT CAST(:j AS varchar), max(seq) + 1, 'queued', :s, :t "
                    "FROM job_transition WHERE job_id = :j"
                ),
                {"j": job_id, "s": state, "t": T_END},
            )
            # the job is touched an hour after it ended (e.g. a late cancel
            # request on a terminal job): updated_at moves, the end does not
            await conn.execute(
                text("UPDATE job SET state = :s, updated_at = :later WHERE id = :j"),
                {"j": job_id, "s": state, "later": T_END + dt.timedelta(hours=1)},
            )
    assert await store.sync(campaign_id) == 2
    one, two = (
        await _run(store, campaign_id, first.id),
        await _run(store, campaign_id, second.id),
    )
    assert (one.state, two.state) == ("done", "failed")
    assert one.ended_at == T_END and two.ended_at == T_END


async def test_ended_at_falls_back_to_updated_at_without_history(
    engine: AsyncEngine,
) -> None:
    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _campaign(store)
    run = (await store.runs(campaign_id, limit=1))[0]
    job_id = await _job(jobs, run)
    assert await store.mark_submitted(run.id, job_id)
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM job_transition WHERE job_id = :j"), {"j": job_id}
        )
        await conn.execute(
            text("UPDATE job SET state = 'succeeded', updated_at = :t WHERE id = :j"),
            {"j": job_id, "t": T_END},
        )
    assert await store.sync(campaign_id) == 1
    assert (await _run(store, campaign_id, run.id)).ended_at == T_END


async def test_runs_can_exclude_nodes(engine: AsyncEngine) -> None:
    store = BenchStore(engine)
    campaign_id = await _campaign(store)
    every = await store.runs(campaign_id, state="planned", limit=100)
    on_a = await store.runs(
        campaign_id, state="planned", limit=100, exclude_nodes=["node_b"]
    )
    assert on_a == [r for r in every if r.node_id == "node_a"]
    assert on_a and len(on_a) < len(every)
    none = await store.runs(
        campaign_id, state="planned", limit=100, exclude_nodes=["node_a", "node_b"]
    )
    assert none == []
