"""V0.6 G3 fix round 1, real PostgreSQL: D126 disk_full by placement, D127
cancel precedence, the root cause kept in a failed job's detail.

Every outcome here is decided in the one transaction that ends the attempt
(ADR-0015 decision 3) - checked by the attempt count and the job history.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from mf_reports import apply_as_placed
from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchDecision,
    DispatchFailure,
    ExecutionStore,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.queue_reads import FabricRows
from meshforge.controller.jobs.retry import RetryReason
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

DIGEST = "sha256:" + "cd" * 32
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}
FAR = dt.timedelta(hours=1)


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=8)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _node(pg: DatabaseSettings) -> str:
    engine = create_engine(pg)
    try:
        node = str(new_node_id())
        await Registry(engine).upsert_node_on_hello(
            node_id=node,
            hostname="h",
            os="linux",
            platform="x86_64",
            hardware={"capabilities": ["exec", "exec-subprocess"]},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
            cpu_slots=4,
            gpu_slots=0,
        )
        return node
    finally:
        await engine.dispose()


async def _queued(
    store: ExecutionStore, pinned: str | None = None, **sched: Any
) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload" if pinned else "auto", "node_id": pinned},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(**{"retry_backoff_s": 0, **sched}),
    )
    return job


class Decide:
    def __init__(self, node: str, fail: DispatchFailure | None = None) -> None:
        self.node, self.fail = node, fail

    def __call__(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        if self.fail is not None:
            return self.fail
        return Dispatch(
            attempt_id=str(new_attempt_id()), start_snapshot={}, node_id=self.node
        )


async def _tick(store: ExecutionStore, decide: Decide) -> list[Any]:
    return await store.dispatch_planned(
        decide,
        limit=10,
        max_per_node=8,
        now=dt.datetime.now(dt.UTC) + FAR,
        aging_s=600.0,
    )


async def _running(store: ExecutionStore, job: JobRecord, node: str) -> Any:
    (mine,) = [r for r in await _tick(store, Decide(node)) if r.job.id == job.id]
    out = await apply_as_placed(
        store,
        StatusReport(mine.attempt.id, mine.attempt.fencing_token, AttemptState.RUNNING),
    )
    assert out.kind is StatusKind.APPLIED
    return out.attempt


def _fail(a: Any, failure: FailureType, retryable: bool | None = False) -> Any:
    return StatusReport(
        a.id,
        a.fencing_token,
        AttemptState.FAILED,
        failure_type=failure,
        detail="boom",
        retryable=retryable,
    )


async def _attempts(pg: DatabaseSettings, job_id: str) -> list[tuple[str, str]]:
    rows = await _sql(
        pg,
        "SELECT state, failure_type FROM attempt WHERE job_id = :j "
        "ORDER BY fencing_token",
        j=job_id,
    )
    return [tuple(r) for r in rows]


async def _states(store: ExecutionStore, job_id: str) -> list[str]:
    return [t.value for _f, t, _ in await store.history(job_id)]


# ------------------------------------------------------------------ D126


async def test_disk_full_of_an_auto_job_is_retried_in_the_same_transaction(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store)
    a = await _running(store, job, node)
    out = await apply_as_placed(store, _fail(a, FailureType.DISK_FULL, retryable=False))
    assert out.retry is not None and out.retry.reason is RetryReason.RETRY
    assert out.job is not None and out.job.state is JobState.QUEUED
    assert (await _states(store, job.id))[-3:] == ["retrying", "planning", "queued"]
    assert await _attempts(pg_settings, job.id) == [("failed", "disk_full")]


async def test_disk_full_of_a_pinned_job_fails_it_with_one_attempt(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store, pinned=node)
    a = await _running(store, job, node)
    out = await apply_as_placed(store, _fail(a, FailureType.DISK_FULL))
    assert out.retry is not None and out.retry.reason is RetryReason.NOT_RETRYABLE
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.job.failure_type is FailureType.DISK_FULL
    assert "retrying" not in await _states(store, job.id)
    assert [r for r in await _tick(store, Decide(node)) if r.job.id == job.id] == []


# ------------------------------------------------------------------ D127


async def test_a_failure_after_a_cancel_request_ends_the_job_cancelled(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store)
    a = await _running(store, job, node)
    cancel = await store.request_cancel(job.id)
    assert cancel is not None and not cancel.cancelled_now
    out = await apply_as_placed(store, _fail(a, FailureType.TIMEOUT, retryable=True))
    assert out.retry is not None and out.retry.reason is RetryReason.CANCELLED
    assert out.job is not None and out.job.state is JobState.CANCELLED
    assert out.job.failure_type is FailureType.CANCELLED
    assert "the attempt ended timeout" in (out.job.failure_detail or "")
    assert await _attempts(pg_settings, job.id) == [("failed", "timeout")]
    assert "retrying" not in await _states(store, job.id)
    assert [r for r in await _tick(store, Decide(node)) if r.job.id == job.id] == []


async def test_a_backstop_failure_after_a_cancel_request_ends_cancelled(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store, max_attempts=1, on_lost_retry=False)
    a = await _running(store, job, node)
    await store.request_cancel(job.id)
    out = await store.end_attempt(
        a.id,
        a.fencing_token,
        event=AttemptEvent.FAIL,
        failure_type=FailureType.TIMEOUT,
        detail="time limit backstop",
    )
    assert out.job is not None and out.job.state is JobState.CANCELLED


async def test_a_failure_racing_a_cancel_request_never_ends_failed(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    for _ in range(20):
        job = await _queued(store)
        a = await _running(store, job, node)
        await asyncio.gather(
            store.request_cancel(job.id),
            apply_as_placed(store, _fail(a, FailureType.DISK_FULL)),
        )
        got = await store.get_job(job.id)
        assert got is not None and got.state is JobState.CANCELLED, got
        assert len(await _attempts(pg_settings, job.id)) == 1


# ------------------------------------------------------------ root cause


async def test_a_failed_retry_keeps_the_first_failure_type_in_the_detail(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store)
    a = await _running(store, job, node)
    await apply_as_placed(store, _fail(a, FailureType.INTEGRITY_ERROR, retryable=True))
    b = await _running(store, job, node)
    out = await apply_as_placed(store, _fail(b, FailureType.NOT_FOUND))
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.job.failure_type is FailureType.NOT_FOUND
    assert out.job.failure_detail == "first failure: integrity_error; boom"


async def test_an_exhausted_retry_of_the_same_type_has_no_prefix(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store, max_attempts=2)
    a = await _running(store, job, node)
    await apply_as_placed(store, _fail(a, FailureType.TIMEOUT, retryable=True))
    b = await _running(store, job, node)
    out = await apply_as_placed(store, _fail(b, FailureType.TIMEOUT, retryable=True))
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.job.failure_detail == "retries_exhausted after 2 attempts: boom"


async def test_a_retried_job_refused_by_the_planner_keeps_its_root_cause(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store)
    a = await _running(store, job, node)
    await apply_as_placed(store, _fail(a, FailureType.DISK_FULL))
    gone = DispatchFailure(FailureType.NODE_LOST, "placed_node_offline")
    await _tick(store, Decide(node, fail=gone))
    got = await store.get_job(job.id)
    assert got is not None and got.state is JobState.FAILED
    assert got.failure_type is FailureType.NODE_LOST
    assert got.failure_detail == "first failure: disk_full; placed_node_offline"
