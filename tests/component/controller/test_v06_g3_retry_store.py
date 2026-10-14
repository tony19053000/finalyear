"""V0.6 G3 on real PostgreSQL: the retry engine (D110, D115, ADR-0015).

* an attempt's end and its job's ``RETRYING -> PLANNING -> QUEUED`` (with
  ``not_before``) are **one transaction** - shown by terminating the
  connection between the statements (``pg_terminate_backend``): nothing of
  either survives, and the next try retries exactly once;
* agent-reported retryable failures, controller ``LOST`` and the
  per-instance ``retryable`` flag; ``on_lost: false``; ``retries_exhausted``
  after ``max_attempts``; non-retryable failures fail at once;
* the dispatch tick sees the job's failed attempts (exclusion input) and
  respects ``not_before``; a new attempt gets the next fencing token;
* fencing on a retried job: the superseded attempt's late report never
  moves the job - even if its row were (wrongly) still live;
* ``requeue_stranded``: a job left ``RETRYING``/``PLANNING`` goes back to
  ``QUEUED`` (D115 safety net).
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
from meshforge.controller.jobs import execution_store as store_module
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchDecision,
    ExecutionStore,
    StatusKind,
    StatusReport,
    Wait,
)
from meshforge.controller.jobs.queue_reads import FabricRows
from meshforge.controller.jobs.retry import RetryReason
from meshforge.controller.jobs.store import JobRecord, JobScheduling, JobStoreError
from meshforge.controller.jobs.transitions import AttemptEvent, JobEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}
RETRY_ARROWS = [
    ("running", "retrying"),
    ("retrying", "planning"),
    ("planning", "queued"),
]


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
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


async def _nodes(pg: DatabaseSettings, count: int = 2) -> list[str]:
    engine = create_engine(pg)
    try:
        ids = []
        for _ in range(count):
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
            ids.append(node)
        return sorted(ids)
    finally:
        await engine.dispose()


async def _queued(store: ExecutionStore, **scheduling: Any) -> JobRecord:
    sched = {"retry_backoff_s": 0, **scheduling}
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(**sched),
    )
    return job


class OnNode:
    """Decider: dispatch every job to ``node``; records what it saw."""

    def __init__(self, node: str) -> None:
        self.node = node
        self.fabrics: list[FabricRows] = []

    def __call__(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        self.fabrics.append(fabric)
        return Dispatch(
            attempt_id=str(new_attempt_id()), start_snapshot={}, node_id=self.node
        )


async def _tick(
    store: ExecutionStore, decide: Any, now: dt.datetime | None = None
) -> list[Any]:
    return await store.dispatch_planned(
        decide,
        limit=10,
        max_per_node=8,
        now=now or dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
        aging_s=600.0,
    )


async def _running(store: ExecutionStore, node: str, **scheduling: Any) -> Any:
    """A job whose (first) attempt is RUNNING on ``node``."""
    job = await _queued(store, **scheduling)
    results = await _tick(store, OnNode(node))
    (mine,) = [r for r in results if r.job.id == job.id]
    return await _run(store, mine.attempt)


async def _run(store: ExecutionStore, attempt: Any) -> Any:
    outcome = await apply_as_placed(
        store, StatusReport(attempt.id, attempt.fencing_token, AttemptState.RUNNING)
    )
    assert outcome.kind is StatusKind.APPLIED
    return outcome.attempt


def _fail(
    attempt: Any, failure: FailureType, retryable: bool | None = None
) -> StatusReport:
    return StatusReport(
        attempt.id,
        attempt.fencing_token,
        AttemptState.FAILED,
        failure_type=failure,
        detail="boom",
        retryable=retryable,
    )


async def _arrows(store: ExecutionStore, job_id: str) -> list[tuple[str | None, str]]:
    return [
        (None if f is None else f.value, t.value)
        for f, t, _ in await store.history(job_id)
    ]


# ------------------------------------------------------------------ retry


async def test_a_retryable_failure_requeues_the_job_in_the_same_transaction(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    attempt = await _running(store, node, retry_backoff_s=7)
    outcome = await apply_as_placed(
        store, _fail(attempt, FailureType.TRANSFER_ERROR, True)
    )
    assert outcome.kind is StatusKind.APPLIED and outcome.ended
    assert outcome.retry is not None and outcome.retry.retry
    assert outcome.retry.backoff_s == 7.0
    assert outcome.attempt is not None and outcome.attempt.state is AttemptState.FAILED
    job = outcome.job
    assert job is not None and job.state is JobState.QUEUED
    assert job.failure_type is None and job.attempt_epoch == 1
    assert (await _arrows(store, job.id))[-3:] == RETRY_ARROWS
    (row,) = await _sql(
        pg_settings,
        "SELECT not_before - clock_timestamp(), (SELECT count(DISTINCT at) FROM "
        "job_transition WHERE job_id = :j AND to_state IN ('retrying','planning',"
        "'queued') AND seq > 4), (SELECT ended_at FROM attempt WHERE id = :a) <= "
        "(SELECT max(at) FROM job_transition WHERE job_id = :j) FROM job WHERE id = :j",
        j=job.id,
        a=attempt.id,
    )
    assert dt.timedelta(seconds=5) < row[0] <= dt.timedelta(seconds=7)
    assert row[2] is True


async def test_not_before_is_respected_and_the_next_attempt_gets_token_2(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node, other = await _nodes(pg_settings)
    attempt = await _running(store, node, retry_backoff_s=60)
    await apply_as_placed(store, _fail(attempt, FailureType.TIMEOUT))
    decide = OnNode(other)
    assert await _tick(store, decide) == []  # backing off: not even offered
    later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=61)
    (result,) = await _tick(store, decide, now=later)
    assert result.attempt.fencing_token == 2 and result.attempt.node_id == other
    (past,) = decide.fabrics[-1].past_failures[result.job.id]
    assert (past.node_id, past.failure_type) == (node, "timeout")
    assert past.ended_at is not None


async def test_a_controller_lost_attempt_retries_unless_on_lost_is_off(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    attempt = await _running(store, node)
    lost = await store.end_attempt(
        attempt.id,
        attempt.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="node_offline",
    )
    assert lost.attempt is not None and lost.attempt.state is AttemptState.LOST
    assert lost.retry is not None and lost.retry.retry
    assert lost.job is not None and lost.job.state is JobState.QUEUED

    attempt = await _running(store, node, on_lost_retry=False)
    kept = await store.end_attempt(
        attempt.id,
        attempt.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="node_offline",
    )
    assert kept.retry is not None
    assert kept.retry.reason is RetryReason.ON_LOST_DISABLED
    assert kept.job is not None and kept.job.state is JobState.FAILED
    assert kept.job.failure_type is FailureType.NODE_LOST


async def test_the_per_instance_flag_and_non_retryable_failures(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    blip = await _running(store, node)
    out = await apply_as_placed(store, _fail(blip, FailureType.OUTPUT_ERROR, True))
    assert out.job is not None and out.job.state is JobState.QUEUED
    refused = await _running(store, node)
    out = await apply_as_placed(
        store, _fail(refused, FailureType.TRANSFER_ERROR, False)
    )
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.job.failure_type is FailureType.TRANSFER_ERROR
    crash = await _running(store, node)
    out = await apply_as_placed(store, _fail(crash, FailureType.USER_ERROR))
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.retry is not None and out.retry.reason is RetryReason.NOT_RETRYABLE
    assert out.job.failure_detail == "boom"


async def test_the_last_allowed_attempt_fails_the_job_retries_exhausted(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node, other = await _nodes(pg_settings)
    attempt = await _running(store, node, max_attempts=2)
    await apply_as_placed(store, _fail(attempt, FailureType.INTEGRITY_ERROR))
    (result,) = await _tick(store, OnNode(other))
    second = await _run(store, result.attempt)
    assert second.fencing_token == 2
    out = await apply_as_placed(store, _fail(second, FailureType.INTEGRITY_ERROR))
    assert out.retry is not None
    assert out.retry.reason is RetryReason.RETRIES_EXHAUSTED
    job = out.job
    assert job is not None and job.state is JobState.FAILED
    assert job.failure_type is FailureType.INTEGRITY_ERROR
    assert job.failure_detail == "retries_exhausted after 2 attempts: boom"
    rows = await _sql(
        pg_settings,
        "SELECT fencing_token, state, failure_type FROM attempt WHERE job_id = :j "
        "ORDER BY fencing_token",
        j=job.id,
    )
    assert [tuple(r) for r in rows] == [
        (1, "failed", "integrity_error"),
        (2, "failed", "integrity_error"),
    ]


async def test_a_cancel_request_stops_retries(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    attempt = await _running(store, node)
    cancel = await store.request_cancel(attempt.job_id)
    assert cancel is not None and cancel.attempt is not None
    out = await apply_as_placed(store, _fail(attempt, FailureType.TIMEOUT))
    assert out.job is not None and out.job.state is JobState.CANCELLED  # D127
    assert out.retry is not None and out.retry.reason is RetryReason.CANCELLED


async def test_a_lost_upload_retries_from_collecting(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    attempt = await _running(store, node)
    up = await apply_as_placed(
        store, StatusReport(attempt.id, attempt.fencing_token, AttemptState.UPLOADING)
    )
    assert up.job is not None and up.job.state is JobState.COLLECTING
    out = await store.end_attempt(
        attempt.id,
        attempt.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="node_offline",
    )
    assert out.job is not None and out.job.state is JobState.QUEUED
    assert (await _arrows(store, out.job.id))[-3:] == [
        ("collecting", "retrying"),
        ("retrying", "planning"),
        ("planning", "queued"),
    ]


# ------------------------------------------------------------- fencing


async def test_the_superseded_attempt_never_moves_the_retried_job(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node, other = await _nodes(pg_settings)
    old = await _running(store, node)
    await store.end_attempt(
        old.id,
        old.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="node_offline",
    )
    (result,) = await _tick(store, OnNode(other))
    new = await _run(store, result.attempt)
    # the partitioned agent comes back and reports DONE for the old attempt
    late = await apply_as_placed(
        store, StatusReport(old.id, old.fencing_token, AttemptState.DONE)
    )
    assert late.kind is StatusKind.FENCED
    # even if the old row were (wrongly) still live, the job's epoch fences it
    await _sql(
        pg_settings, "UPDATE attempt SET state = 'uploading' WHERE id = :a", a=old.id
    )
    late = await apply_as_placed(
        store, StatusReport(old.id, old.fencing_token, AttemptState.DONE)
    )
    assert late.kind is StatusKind.FENCED
    gone = await store.end_attempt(
        old.id,
        old.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="node_offline",
    )
    assert gone.retry is None  # ended, but the job is not its to move
    job = await store.get_job(new.job_id)
    assert job is not None and job.state is JobState.RUNNING
    assert job.attempt_epoch == 2
    done = await apply_as_placed(
        store, StatusReport(new.id, new.fencing_token, AttemptState.DONE)
    )
    assert done.job is not None and done.job.state is JobState.SUCCEEDED


# ------------------------------------------------- one transaction (D115)


async def test_killing_the_connection_between_end_and_requeue_leaves_neither(
    store: ExecutionStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (node, _) = await _nodes(pg_settings)
    attempt = await _running(store, node)
    real = store_module._move_job
    killed: list[JobEvent] = []

    async def kill_at_plan(db: Any, row: Any, event: JobEvent, **kw: Any) -> bool:
        if event is JobEvent.PLAN and not killed:
            killed.append(event)
            # the controller "dies" here: its connection is gone mid-transaction
            await db.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
        return await real(db, row, event, **kw)

    monkeypatch.setattr(store_module, "_move_job", kill_at_plan)
    with pytest.raises(JobStoreError):
        await apply_as_placed(store, _fail(attempt, FailureType.TIMEOUT))
    assert killed == [JobEvent.PLAN]
    rows = await _sql(
        pg_settings,
        "SELECT a.state, j.state, j.not_before, (SELECT count(*) FROM job_transition "
        "t WHERE t.job_id = j.id AND t.to_state = 'retrying') FROM attempt a JOIN "
        "job j ON j.id = a.job_id WHERE a.id = :a",
        a=attempt.id,
    )
    assert [tuple(r) for r in rows] == [("running", "running", None, 0)]
    # the agent replays its un-acked report: the retry happens exactly once
    out = await apply_as_placed(store, _fail(attempt, FailureType.TIMEOUT))
    assert out.job is not None and out.job.state is JobState.QUEUED
    assert (await _arrows(store, out.job.id)).count(("running", "retrying")) == 1


async def test_killing_the_connection_inside_a_controller_loss_leaves_neither(
    store: ExecutionStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (node, _) = await _nodes(pg_settings)
    attempt = await _running(store, node)
    real = store_module._move_job

    async def kill_at_planned(db: Any, row: Any, event: JobEvent, **kw: Any) -> bool:
        if event is JobEvent.PLANNED:
            await db.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
        return await real(db, row, event, **kw)

    monkeypatch.setattr(store_module, "_move_job", kill_at_planned)
    with pytest.raises(JobStoreError):
        await store.end_attempt(
            attempt.id,
            attempt.fencing_token,
            event=AttemptEvent.LOSE,
            failure_type=FailureType.NODE_LOST,
            detail="node_offline",
        )
    rows = await _sql(
        pg_settings,
        "SELECT a.state, j.state FROM attempt a JOIN job j ON j.id = a.job_id "
        "WHERE a.id = :a",
        a=attempt.id,
    )
    assert [tuple(r) for r in rows] == [("running", "running")]


# ---------------------------------------------------- stranded (D115)


async def test_stranded_retrying_and_planning_jobs_are_requeued(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    retrying = await _queued(store)
    planning = await _queued(store)
    await _sql(
        pg_settings,
        "UPDATE job SET state = 'retrying' WHERE id = :a; ",
        a=retrying.id,
    )
    await _sql(
        pg_settings, "UPDATE job SET state = 'planning' WHERE id = :b", b=planning.id
    )
    assert sorted(await store.requeue_stranded()) == sorted([retrying.id, planning.id])
    for job_id in (retrying.id, planning.id):
        job = await store.get_job(job_id)
        assert job is not None and job.state is JobState.QUEUED
    assert (await _arrows(store, retrying.id))[-2:] == [
        ("retrying", "planning"),
        ("planning", "queued"),
    ]
    assert await store.requeue_stranded() == []
    assert isinstance(Wait(), Wait)


async def test_the_failed_attempt_keeps_its_own_queue_phase(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    """The terminal phase rows are computed before the retry's new QUEUED
    entry exists: attempt 1's queue phase is its own wait, not skipped."""
    node, other = await _nodes(pg_settings)
    attempt = await _running(store, node)
    await apply_as_placed(store, _fail(attempt, FailureType.TIMEOUT))
    lost_job = await _running(store, node)
    await store.end_attempt(
        lost_job.id,
        lost_job.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="node_offline",
    )
    for ended in (attempt, lost_job):
        phases = {p.phase: p for p in await store.phases(ended.id)}
        assert set(phases) == {"queue", "stage", "env", "run", "collect"}
        assert phases["queue"].skipped is False, phases["queue"]
    # the retry was dispatched by the second job's tick (backoff 0)
    retried = [
        a for a in await store.list_attempts(attempt.job_id) if a.fencing_token == 2
    ]
    if not retried:
        retried = [
            r.attempt
            for r in await _tick(store, OnNode(other))
            if r.job.id == attempt.job_id
        ]
    second = await _run(store, retried[0])
    await apply_as_placed(store, StatusReport(second.id, 2, AttemptState.UPLOADING))
    await apply_as_placed(store, StatusReport(second.id, 2, AttemptState.DONE))
    phases = {p.phase: p for p in await store.phases(second.id)}
    assert phases["queue"].skipped is False
    first = {p.phase: p for p in await store.phases(attempt.id)}
    # the retry's queue wait is its own (it started after the first one ended)
    assert phases["queue"].started_at > first["queue"].started_at
