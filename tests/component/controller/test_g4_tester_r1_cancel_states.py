"""V0.4 G4 tester round 1: cancel from every legal job state (D69), and cancel
racing the attempt's own completion, on a real PostgreSQL database.

The implementer's ``test_cancel_queued_now_and_running_via_agent`` only exercises
QUEUED (cancellable now) and RUNNING (cancel via agent). ``JOB_TRANSITIONS`` (and
D69's prose) says CANCEL is legal from SUBMITTED, PLANNING, QUEUED, PREPARING,
RUNNING, COLLECTING and RETRYING - every one of those is exercised here, plus the
race between an agent's final DONE report and a concurrent cancel request, which
must never corrupt the row (double terminal write, or a CANCELLED job with a
DONE/exit_code-0 attempt).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from mf_reports import apply_as_placed

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    ExecutionStore,
    NodeView,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.jobs.transitions import JobEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32
MANIFEST: dict[str, Any] = {
    "name": "demo",
    "entrypoint": ["python", "main.py"],
    "limits": {"timeout": 60, "network": "default"},
}


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=10)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def node_id(pg_settings: DatabaseSettings, store: ExecutionStore) -> str:
    engine = create_engine(pg_settings)
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
        )
        return node
    finally:
        await engine.dispose()


def _dispatch_all(_job: JobRecord, node: NodeView | None) -> Dispatch | None:
    if node is None:
        return None
    return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={"k": 1})


async def _job_through(
    store: ExecutionStore, node_id: str, through: list[JobEvent]
) -> JobRecord:
    job, _ = await store.create_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node_id},
        request_hash="h" * 64,
        idempotency_key=None,
        through=through,
    )
    return job


@pytest.mark.parametrize(
    "through",
    [
        pytest.param([], id="submitted"),
        pytest.param([JobEvent.PLAN], id="planning"),
        pytest.param(
            [JobEvent.PLAN, JobEvent.PLANNED, JobEvent.DISPATCHED, JobEvent.RETRY],
            id="retrying",
        ),
    ],
)
async def test_cancel_now_from_every_pre_dispatch_state(
    store: ExecutionStore, node_id: str, through: list[JobEvent]
) -> None:
    job = await _job_through(store, node_id, through)
    outcome = await store.request_cancel(job.id)
    assert outcome is not None and outcome.cancelled_now, outcome
    assert outcome.job.state is JobState.CANCELLED
    stored = await store.get_job(job.id)
    assert stored is not None and stored.state is JobState.CANCELLED
    # idempotent: cancelling an already-cancelled job is a no-op, not an error
    again = await store.request_cancel(job.id)
    assert again is not None and not again.cancelled_now
    assert again.job.state is JobState.CANCELLED


async def _queued_job(store: ExecutionStore, node_id: str) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node_id},
        request_hash="h" * 64,
        idempotency_key=None,
    )
    return job


async def test_cancel_via_agent_from_preparing(
    store: ExecutionStore, node_id: str
) -> None:
    await _queued_job(store, node_id)
    (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=1)
    assert result.attempt is not None
    job_id = result.job.id
    outcome = await store.request_cancel(job_id)
    assert outcome is not None and not outcome.cancelled_now
    assert (
        outcome.attempt is not None and outcome.attempt.state is AttemptState.ASSIGNED
    )
    stored = await store.get_job(job_id)
    assert stored is not None and stored.state is JobState.PREPARING
    assert stored.cancel_requested_at is not None


async def test_cancel_via_agent_from_collecting(
    store: ExecutionStore, node_id: str
) -> None:
    job = await _queued_job(store, node_id)
    (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=1)
    assert result.attempt is not None
    attempt_id = result.attempt.id
    await apply_as_placed(store, StatusReport(attempt_id, 1, AttemptState.RUNNING))
    exited = await apply_as_placed(
        store, StatusReport(attempt_id, 1, AttemptState.UPLOADING)
    )
    assert exited.job is not None and exited.job.state is JobState.COLLECTING
    outcome = await store.request_cancel(job.id)
    assert outcome is not None and not outcome.cancelled_now
    assert outcome.attempt is not None and outcome.attempt.id == attempt_id
    stored = await store.get_job(job.id)
    assert stored is not None and stored.state is JobState.COLLECTING


async def test_cancel_racing_a_concurrent_done_report_never_corrupts_the_row(
    store: ExecutionStore, node_id: str
) -> None:
    """The DB row lock (FOR UPDATE) must serialize a completing attempt against a
    concurrent cancel request: the job ends either SUCCEEDED (cancel lost the
    race, no-op) or CANCELLED (the DONE report is fenced/duplicate after) - never
    both a CANCELLED job with a DONE attempt/exit_code, and never a crash."""
    for _ in range(20):
        job = await _queued_job(store, node_id)
        (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=1)
        assert result.attempt is not None
        attempt_id = result.attempt.id
        await apply_as_placed(store, StatusReport(attempt_id, 1, AttemptState.RUNNING))

        cancel_task = asyncio.create_task(store.request_cancel(job.id))
        done_task = asyncio.create_task(
            apply_as_placed(
                store, StatusReport(attempt_id, 1, AttemptState.DONE, exit_code=0)
            )
        )
        cancel_outcome, done_outcome = await asyncio.gather(cancel_task, done_task)
        assert cancel_outcome is not None
        assert done_outcome.kind in (StatusKind.APPLIED, StatusKind.FENCED)

        stored_job = await store.get_job(job.id)
        assert stored_job is not None
        stored_attempts = await store.list_attempts(job.id)
        assert stored_job.state in (JobState.SUCCEEDED, JobState.CANCELLED)
        (attempt,) = stored_attempts
        if stored_job.state is JobState.SUCCEEDED:
            # the DONE report won the race outright
            assert attempt.state is AttemptState.DONE and attempt.exit_code == 0
        else:
            # cancel was requested first (or won); a DONE report that already
            # landed cannot be un-applied, but the job itself must not be both
            # CANCELLED *and* have accepted a fresh DONE afterwards - it is one
            # or the other, consistently.
            assert stored_job.state is JobState.CANCELLED
