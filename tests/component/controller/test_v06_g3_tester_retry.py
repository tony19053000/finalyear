"""V0.6 G3 tester round 1, real PostgreSQL: try to break the retry engine.

* the controller connection is terminated before EVERY SQL statement of the
  retry transaction (not just one) - nothing half-done may survive, and the
  replay retries exactly once;
* double / concurrent loss of one attempt retries once;
* late reports of a superseded or lost attempt never move the job;
* retries_exhausted boundaries (1 and 10 attempts), loss on the last attempt;
* on_lost:false; cancel in QUEUED(not_before)/after a request; not_before and
  doubling backoff as stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from mf_reports import apply_as_placed
from sqlalchemy import event, text

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchDecision,
    ExecutionStore,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.queue_reads import FabricRows
from meshforge.controller.jobs.retry import RetryReason
from meshforge.controller.jobs.store import JobRecord, JobScheduling, JobStoreError
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

DIGEST = "sha256:" + "ab" * 32
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}


@pytest_asyncio.fixture
async def engine_store(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[tuple[Any, ExecutionStore]]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=8)
    try:
        yield engine, ExecutionStore(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def store(engine_store: tuple[Any, ExecutionStore]) -> ExecutionStore:
    return engine_store[1]


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
    def __init__(self, node: str) -> None:
        self.node = node

    def __call__(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        return Dispatch(
            attempt_id=str(new_attempt_id()), start_snapshot={}, node_id=self.node
        )


async def _tick(
    store: ExecutionStore, node: str, at: dt.datetime | None = None
) -> list[Any]:
    return await store.dispatch_planned(
        OnNode(node),
        limit=10,
        max_per_node=8,
        now=at or dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
        aging_s=600.0,
    )


async def _run(store: ExecutionStore, attempt: Any) -> Any:
    out = await apply_as_placed(
        store, StatusReport(attempt.id, attempt.fencing_token, AttemptState.RUNNING)
    )
    assert out.kind is StatusKind.APPLIED
    return out.attempt


async def _running(store: ExecutionStore, node: str, **sched: Any) -> Any:
    job = await _queued(store, **sched)
    results = await _tick(store, node)
    (mine,) = [r for r in results if r.job.id == job.id]
    return await _run(store, mine.attempt)


def _fail(a: Any, failure: FailureType, retryable: bool | None = None) -> StatusReport:
    return StatusReport(
        a.id,
        a.fencing_token,
        AttemptState.FAILED,
        failure_type=failure,
        detail="boom",
        retryable=retryable,
    )


async def _lose(store: ExecutionStore, a: Any, detail: str = "node_offline") -> Any:
    return await store.end_attempt(
        a.id,
        a.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail=detail,
    )


async def _arrows(store: ExecutionStore, job_id: str) -> list[tuple[str | None, str]]:
    return [
        (None if f is None else f.value, t.value)
        for f, t, _ in await store.history(job_id)
    ]


async def _snapshot(pg: DatabaseSettings, job_id: str) -> tuple[Any, ...]:
    rows = await _sql(
        pg,
        "SELECT j.state, j.attempt_epoch, j.not_before IS NOT NULL, "
        "(SELECT count(*) FROM job_transition t WHERE t.job_id = j.id), "
        "(SELECT string_agg(a.state, ',' ORDER BY a.fencing_token) FROM attempt a "
        "WHERE a.job_id = j.id), "
        "(SELECT count(*) FROM attempt_phase p JOIN attempt a ON a.id = p.attempt_id "
        "WHERE a.job_id = j.id) FROM job j WHERE j.id = :j",
        j=job_id,
    )
    return tuple(rows[0])


# ---------------------------------------- kill before every statement


async def _kill_every_statement(
    engine: Any,
    store: ExecutionStore,
    pg: DatabaseSettings,
    *,
    make_attempt: Any,
    act: Any,
    final_job_state: JobState,
) -> int:
    """For k = 1, 2, ...: run ``act`` on a fresh attempt but terminate the
    backend just before statement k. Each time nothing may have changed;
    replaying then yields the full outcome exactly once. Returns the number
    of statements the transaction has."""
    k = 0
    while True:
        k += 1
        attempt = await make_attempt()
        before = await _snapshot(pg, attempt.job_id)
        counter = {"n": 0, "killed": False}

        def hook(
            conn: Any,
            cursor: Any,
            statement: str,
            *a: Any,
            _c: Any = counter,
            _k: int = k,
        ) -> None:
            _c["n"] += 1
            if _c["n"] == _k:
                _c["killed"] = True
                with contextlib.suppress(Exception):
                    cursor.execute("SELECT pg_terminate_backend(pg_backend_pid())")

        event.listen(engine.sync_engine, "before_cursor_execute", hook)
        try:
            try:
                await act(attempt)
                raised = False
            except JobStoreError:
                raised = True
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", hook)
        if not counter["killed"]:
            assert not raised
            job = await store.get_job(attempt.job_id)
            assert job is not None and job.state is final_job_state
            return k - 1
        assert raised, f"statement {k} was killed but the call did not fail"
        assert await _snapshot(pg, attempt.job_id) == before, f"half-done at stmt {k}"
        # the agent / sweeper replays: the outcome happens exactly once
        await act(attempt)
        job = await store.get_job(attempt.job_id)
        assert job is not None and job.state is final_job_state
        arrows = await _arrows(store, attempt.job_id)
        assert arrows.count(("running", "retrying")) <= 1
        assert arrows.count(("retrying", "planning")) <= 1
        rows = await _sql(
            pg,
            "SELECT count(*) FROM attempt WHERE job_id = :j",
            j=attempt.job_id,
        )
        assert rows[0][0] == 1
        await store.request_cancel(attempt.job_id)  # keep the queue clean


async def test_kill_before_every_statement_of_a_reported_retry(
    engine_store: tuple[Any, ExecutionStore], pg_settings: DatabaseSettings
) -> None:
    engine, store = engine_store
    (node, _) = await _nodes(pg_settings)

    async def make() -> Any:
        return await _running(store, node)

    async def act(a: Any) -> None:
        await apply_as_placed(store, _fail(a, FailureType.TRANSFER_ERROR, True))

    n = await _kill_every_statement(
        engine,
        store,
        pg_settings,
        make_attempt=make,
        act=act,
        final_job_state=JobState.QUEUED,
    )
    assert n >= 6  # it really is a multi-statement transaction


async def test_kill_before_every_statement_of_a_controller_loss(
    engine_store: tuple[Any, ExecutionStore], pg_settings: DatabaseSettings
) -> None:
    engine, store = engine_store
    (node, _) = await _nodes(pg_settings)

    async def make() -> Any:
        return await _running(store, node)

    async def act(a: Any) -> None:
        await _lose(store, a)

    n = await _kill_every_statement(
        engine,
        store,
        pg_settings,
        make_attempt=make,
        act=act,
        final_job_state=JobState.QUEUED,
    )
    assert n >= 6


async def test_kill_before_every_statement_of_the_exhausting_failure(
    engine_store: tuple[Any, ExecutionStore], pg_settings: DatabaseSettings
) -> None:
    engine, store = engine_store
    (node, _) = await _nodes(pg_settings)

    async def make() -> Any:
        return await _running(store, node, max_attempts=1)

    async def act(a: Any) -> None:
        await apply_as_placed(store, _fail(a, FailureType.TIMEOUT))

    await _kill_every_statement(
        engine,
        store,
        pg_settings,
        make_attempt=make,
        act=act,
        final_job_state=JobState.FAILED,
    )


# ------------------------------------------------------ double LOST


async def test_losing_the_same_attempt_twice_retries_once(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node)
    first = await _lose(store, a, "node_offline")
    second = await _lose(store, a, "node_not_connected")  # orphan sweep, later
    assert first.retry is not None and first.retry.retry
    assert second.kind is StatusKind.DUPLICATE and second.retry is None
    job = await store.get_job(a.job_id)
    assert job is not None and job.state is JobState.QUEUED
    assert job.attempt_epoch == 1
    arrows = await _arrows(store, a.job_id)
    assert arrows.count(("running", "retrying")) == 1
    assert arrows.count(("planning", "queued")) == 2  # submit + the retry


async def test_concurrent_loss_and_failure_reports_retry_exactly_once(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    for _round in range(5):
        a = await _running(store, node)
        results = await asyncio.gather(
            _lose(store, a, "node_offline"),
            _lose(store, a, "node_not_connected"),
            apply_as_placed(store, _fail(a, FailureType.TIMEOUT)),
            apply_as_placed(store, _fail(a, FailureType.TIMEOUT)),
            _lose(store, a, "agent_lost_attempt"),
        )
        applied = [r for r in results if r.kind is StatusKind.APPLIED]
        assert len(applied) == 1, [r.kind for r in results]
        arrows = await _arrows(store, a.job_id)
        assert arrows.count(("running", "retrying")) == 1
        job = await store.get_job(a.job_id)
        assert job is not None and job.state is JobState.QUEUED
        rows = await _sql(
            pg_settings, "SELECT count(*) FROM attempt WHERE job_id = :j", j=a.job_id
        )
        assert rows[0][0] == 1


# ------------------------------------------- late reports (fencing)


@pytest.mark.parametrize("old_end", ["lost", "failed"])
@pytest.mark.parametrize(
    "late",
    [
        AttemptState.RUNNING,
        AttemptState.UPLOADING,
        AttemptState.DONE,
        AttemptState.FAILED,
    ],
)
async def test_a_late_report_from_the_old_attempt_never_moves_the_job(
    store: ExecutionStore,
    pg_settings: DatabaseSettings,
    old_end: str,
    late: AttemptState,
) -> None:
    node, other = await _nodes(pg_settings)
    old = await _running(store, node)
    if old_end == "lost":
        await _lose(store, old)
    else:
        await apply_as_placed(store, _fail(old, FailureType.TIMEOUT))
    for phase in ("queued", "attempt2_running"):
        if phase == "attempt2_running":
            (r,) = await _tick(store, other)
            await _run(store, r.attempt)
        before = await store.get_job(old.job_id)
        report = StatusReport(
            old.id,
            old.fencing_token,
            late,
            failure_type=FailureType.USER_ERROR
            if late is AttemptState.FAILED
            else None,
            detail="late",
        )
        out = await apply_as_placed(store, report)
        assert out.kind in (StatusKind.FENCED, StatusKind.DUPLICATE), (phase, out.kind)
        after = await store.get_job(old.job_id)
        assert before is not None and after is not None
        assert (after.state, after.attempt_epoch) == (
            before.state,
            before.attempt_epoch,
        )
        assert after.state is (
            JobState.QUEUED if phase == "queued" else JobState.RUNNING
        )


async def test_a_report_with_a_wrong_or_future_token_is_fenced(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node)
    for token in (0, 2, 99, -1):
        out = await apply_as_placed(store, StatusReport(a.id, token, AttemptState.DONE))
        assert out.kind is StatusKind.FENCED
    job = await store.get_job(a.job_id)
    assert job is not None and job.state is JobState.RUNNING


# ------------------------------------------------- exhaustion boundaries


async def test_max_attempts_one_fails_at_the_first_retryable_failure(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node, max_attempts=1)
    out = await apply_as_placed(store, _fail(a, FailureType.TIMEOUT))
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.job.failure_type is FailureType.TIMEOUT
    assert out.job.failure_detail is not None
    assert "retries_exhausted" in out.job.failure_detail


async def test_ten_attempts_then_exhausted_with_doubling_capped_backoff(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node, max_attempts=10, retry_backoff_s=100)
    seen: list[float] = []
    for n in range(1, 10):
        assert a.fencing_token == n
        out = await apply_as_placed(store, _fail(a, FailureType.TIMEOUT))
        assert out.job is not None and out.job.state is JobState.QUEUED, n
        rows = await _sql(
            pg_settings,
            "SELECT extract(epoch FROM not_before - clock_timestamp()) FROM job "
            "WHERE id = :j",
            j=a.job_id,
        )
        seen.append(float(rows[0][0]))
        later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=400)
        (r,) = await _tick(store, node, at=later)
        a = await _run(store, r.attempt)
    # 100, 200, then capped at 300 (+- DB/clock slack)
    for got, want in zip(seen, [100, 200] + [300] * 7, strict=True):
        assert want - 5 < got <= want, (seen, want)
    assert a.fencing_token == 10
    out = await apply_as_placed(store, _fail(a, FailureType.TIMEOUT))
    assert out.retry is not None and out.retry.reason is RetryReason.RETRIES_EXHAUSTED
    assert out.job is not None and out.job.state is JobState.FAILED
    assert "after 10 attempts" in (out.job.failure_detail or "")


async def test_loss_on_the_last_attempt_ends_failed_node_lost_exhausted(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node, other = await _nodes(pg_settings)
    a = await _running(store, node, max_attempts=2)
    await _lose(store, a)
    (r,) = await _tick(store, other)
    b = await _run(store, r.attempt)
    out = await _lose(store, b, "node_offline")
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.job.failure_type is FailureType.NODE_LOST
    assert "retries_exhausted" in (out.job.failure_detail or "")


# -------------------------------------------------------- on_lost:false


async def test_on_lost_false_does_not_block_other_retryable_failures(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node, on_lost_retry=False)
    out = await apply_as_placed(store, _fail(a, FailureType.TIMEOUT))
    assert out.job is not None and out.job.state is JobState.QUEUED
    # agent-reported node_lost (graceful shutdown) is a loss: no retry
    b = await _running(store, node, on_lost_retry=False)
    out = await apply_as_placed(store, _fail(b, FailureType.NODE_LOST))
    assert out.job is not None and out.job.state is JobState.FAILED
    assert out.job.failure_type is FailureType.NODE_LOST


# ----------------------------------------------------------- cancel


async def test_cancel_while_waiting_out_not_before_ends_cancelled_for_good(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node, retry_backoff_s=60)
    await apply_as_placed(store, _fail(a, FailureType.TIMEOUT))
    cancel = await store.request_cancel(a.job_id)
    assert cancel is not None and cancel.cancelled_now
    assert cancel.job.state is JobState.CANCELLED
    far = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    assert await _tick(store, node, at=far) == []
    # late report from the old attempt cannot resurrect it
    out = await apply_as_placed(
        store, StatusReport(a.id, a.fencing_token, AttemptState.DONE)
    )
    assert out.kind in (StatusKind.FENCED, StatusKind.DUPLICATE)
    job = await store.get_job(a.job_id)
    assert job is not None and job.state is JobState.CANCELLED
    again = await store.request_cancel(a.job_id)
    assert again is not None and not again.cancelled_now


async def test_a_loss_after_a_cancel_request_is_not_reported_as_node_lost(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    """The user asked to cancel; the node then died. The job must not retry
    (tested elsewhere) - and the honest end state is CANCELLED, not a
    FAILED(node_lost) the user never asked for."""
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node)
    cancel = await store.request_cancel(a.job_id)
    assert cancel is not None and not cancel.cancelled_now
    out = await _lose(store, a)
    assert out.retry is not None and out.retry.reason is RetryReason.CANCELLED
    assert out.job is not None
    assert out.job.state is JobState.CANCELLED, out.job.state


# --------------------------------------------------- misc invariants


async def test_a_retried_job_keeps_queued_at_and_all_attempt_rows(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node, other = await _nodes(pg_settings)
    job = await _queued(store)
    first = (
        await _sql(pg_settings, "SELECT queued_at FROM job WHERE id=:j", j=job.id)
    )[0][0]
    (r,) = await _tick(store, node)
    a = await _run(store, r.attempt)
    await _lose(store, a)
    again = (
        await _sql(pg_settings, "SELECT queued_at FROM job WHERE id=:j", j=job.id)
    )[0][0]
    assert again == first
    (r2,) = await _tick(store, other)
    assert r2.attempt.fencing_token == 2 and r2.attempt.id != a.id
    rows = await _sql(
        pg_settings,
        "SELECT state, node_id FROM attempt WHERE job_id = :j ORDER BY fencing_token",
        j=job.id,
    )
    assert [tuple(x) for x in rows] == [("lost", node), ("assigned", other)]


async def test_cancel_racing_the_retry_transaction_never_leaves_a_live_retry(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    for _round in range(25):
        a = await _running(store, node)
        cancel, out = await asyncio.gather(
            store.request_cancel(a.job_id),
            apply_as_placed(store, _fail(a, FailureType.TIMEOUT)),
        )
        assert cancel is not None and out.kind is StatusKind.APPLIED
        job = await store.get_job(a.job_id)
        assert job is not None
        # either the cancel request committed first (D127: the failure then
        # ends the job CANCELLED, no retry) or the retry committed first and
        # the cancel then ended the queued job - CANCELLED either way.
        assert job.state is JobState.CANCELLED, job.state
        rows = await _sql(
            pg_settings, "SELECT count(*) FROM attempt WHERE job_id = :j", j=a.job_id
        )
        assert rows[0][0] == 1
        far = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        assert [
            r for r in await _tick(store, node, at=far) if r.job.id == a.job_id
        ] == []


async def test_a_cancelled_report_without_a_cancel_request_is_not_retried(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    (node, _) = await _nodes(pg_settings)
    a = await _running(store, node)
    out = await apply_as_placed(store, _fail(a, FailureType.CANCELLED))
    assert out.job is not None and out.job.state is JobState.CANCELLED
    assert out.retry is None or not out.retry.retry
