"""V0.6 G4 tester round 1 (D129) through the real dispatch tick on PostgreSQL:
a retrying job whose every capable node is excluded waits (no attempt burned,
no decision growth across ticks), and starts the moment a node that would take
it exists - lifted tier by tier, never onto a node that cannot take it."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from test_v06_g4_criterion4 import (
    Manager,
    _digest,
    _job,
    _node,
    _planner,
    _sql,
)

from meshforge.common.ids import new_attempt_id
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _failed_attempt(
    pg: DatabaseSettings, job_id: str, node: str, token: int, ftype: str, state: str
) -> None:
    await _sql(
        pg,
        "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, "
        "failure_type, created_at, updated_at, ended_at) VALUES (:a, :j, :n, :t, "
        ":s, :f, now(), now(), now())",
        a=str(new_attempt_id()),
        j=job_id,
        n=node,
        t=token,
        s=state,
        f=ftype,
    )


async def _epoch(pg: DatabaseSettings, job_id: str, epoch: int) -> None:
    await _sql(pg, "UPDATE job SET attempt_epoch = :e WHERE id = :j", e=epoch, j=job_id)


async def _tick(store: ExecutionStore, manager: Manager) -> list[Any]:
    return list(
        await store.dispatch_planned(
            _planner(manager).decide,
            limit=10,
            max_per_node=8,
            now=dt.datetime.now(dt.UTC),
            aging_s=600.0,
        )
    )


async def _decisions(pg: DatabaseSettings, job_id: str) -> int:
    rows = await _sql(
        pg, "SELECT count(*) FROM plan_decision WHERE job_id = :j", j=job_id
    )
    return int(rows[0][0])


async def test_every_capable_node_excluded_and_unusable_the_job_waits_quietly(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _node(pg_settings, 1.0), await _node(pg_settings, 1.0)
    job = await _job(
        store,
        digest=_digest(5),
        scheduling=JobScheduling(fingerprint="5" * 64, input_bytes=1000),
    )
    await _failed_attempt(pg_settings, job.id, a, 1, "node_lost", "lost")
    await _failed_attempt(pg_settings, job.id, b, 2, "disk_full", "failed")
    await _epoch(pg_settings, job.id, 2)
    # A is offline (nothing usable); B is online but its window has not passed
    manager = Manager([b])
    for _ in range(4):
        assert [r for r in await _tick(store, manager) if r.attempt is not None] == []
    attempts = await _sql(
        pg_settings, "SELECT count(*) FROM attempt WHERE job_id = :j", j=job.id
    )
    assert attempts[0][0] == 2, "an attempt was burned on an excluded node"
    assert await _decisions(pg_settings, job.id) <= 1, "a waiting job grows decisions"
    state = await _sql(pg_settings, "SELECT state FROM job WHERE id = :j", j=job.id)
    assert state[0][0] == "queued"

    # A node that would take it (a recovered node_lost node) is used at once
    manager = Manager([a, b])
    placed = [r for r in await _tick(store, manager) if r.attempt is not None]
    assert [r.attempt.node_id for r in placed] == [a]


async def test_lift_goes_tier_by_tier_and_skips_a_node_that_cannot_take_it(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    lost, transfer, full = (
        await _node(pg_settings, 1.0),
        await _node(pg_settings, 1.0),
        await _node(pg_settings, 1.0),
    )
    job = await _job(
        store,
        digest=_digest(6),
        scheduling=JobScheduling(fingerprint="6" * 64, input_bytes=1000),
    )
    await _failed_attempt(pg_settings, job.id, lost, 1, "node_lost", "lost")
    await _failed_attempt(pg_settings, job.id, transfer, 2, "transfer_error", "failed")
    await _failed_attempt(pg_settings, job.id, full, 3, "disk_full", "failed")
    await _epoch(pg_settings, job.id, 3)
    # lost is offline; transfer + full are online: only `transfer` is liftable
    placed = [
        r
        for r in await _tick(store, Manager([transfer, full]))
        if r.attempt is not None
    ]
    assert [r.attempt.node_id for r in placed] == [transfer]
