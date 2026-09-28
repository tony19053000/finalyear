"""JobStore against real PostgreSQL (D49): CAS races, idempotent create, fencing.

The races are real: several coroutines, each on its own pooled connection, hit
one row at once; PostgreSQL's row locks decide the winner.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptPhase, AttemptState, JobState
from meshforge.controller.jobs.store import (
    IdempotencyConflictError,
    JobStore,
    StaleFencingTokenError,
    StaleStateError,
    UnknownJobError,
)
from meshforge.controller.jobs.transitions import AttemptEvent, JobEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[JobStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=10)
    try:
        yield JobStore(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def node_id(pg_settings: DatabaseSettings, store: JobStore) -> str:
    engine = create_engine(pg_settings)
    try:
        node = str(new_node_id())
        await Registry(engine).upsert_node_on_hello(
            node_id=node,
            hostname="h",
            os="linux",
            platform="x86_64",
            hardware={},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
        )
        return node
    finally:
        await engine.dispose()


async def _create(
    store: JobStore, *, key: str | None = None, request_hash: str = "h" * 64
) -> tuple[str, bool]:
    record, created = await store.create_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": "n"},
        request_hash=request_hash,
        idempotency_key=key,
    )
    return record.id, created


async def test_create_and_walk_to_queued_with_history(store: JobStore) -> None:
    job_id, created = await _create(store)
    assert created
    await store.transition_job(job_id, JobEvent.PLAN, expected=JobState.SUBMITTED)
    record = await store.transition_job(
        job_id, JobEvent.PLANNED, expected=JobState.PLANNING
    )
    assert record.state is JobState.QUEUED
    history = await store.transitions(job_id)
    assert [(t.seq, t.from_state, t.to_state) for t in history] == [
        (1, None, JobState.SUBMITTED),
        (2, JobState.SUBMITTED, JobState.PLANNING),
        (3, JobState.PLANNING, JobState.QUEUED),
    ]
    assert all(t.at.tzinfo is not None for t in history)
    assert history[0].at <= history[1].at <= history[2].at


async def test_cas_race_exactly_one_winner(store: JobStore) -> None:
    for _ in range(10):
        job_id, _ = await _create(store)
        await store.transition_job(job_id, JobEvent.PLAN, expected=JobState.SUBMITTED)
        await store.transition_job(job_id, JobEvent.PLANNED, expected=JobState.PLANNING)
        results = await asyncio.gather(
            store.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED),
            store.transition_job(job_id, JobEvent.DISPATCHED, expected=JobState.QUEUED),
            store.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1, results
        assert all(isinstance(e, StaleStateError) for e in losers), losers
        final = await store.get_job(job_id)
        assert final is not None and final.state is winners[0].state
        history = await store.transitions(job_id)
        assert [t.seq for t in history] == [1, 2, 3, 4]  # one arrow recorded, no gap


async def test_stale_expected_state_and_illegal_and_unknown(store: JobStore) -> None:
    job_id, _ = await _create(store)
    with pytest.raises(StaleStateError):
        await store.transition_job(job_id, JobEvent.PLANNED, expected=JobState.PLANNING)
    with pytest.raises(UnknownJobError):
        await store.transition_job(
            str(new_job_id()), JobEvent.PLAN, expected=JobState.SUBMITTED
        )
    record = await store.get_job(job_id)
    assert record is not None and record.state is JobState.SUBMITTED


async def test_failed_needs_a_failure_type(store: JobStore) -> None:
    job_id, _ = await _create(store)
    await store.transition_job(job_id, JobEvent.PLAN, expected=JobState.SUBMITTED)
    with pytest.raises(ValueError):
        await store.transition_job(
            job_id, JobEvent.NO_FEASIBLE_PLAN, expected=JobState.PLANNING
        )
    failed = await store.transition_job(
        job_id,
        JobEvent.NO_FEASIBLE_PLAN,
        expected=JobState.PLANNING,
        failure_type=FailureType.NO_FEASIBLE_PLAN,
        failure_detail="x\x00" * 2000,
    )
    assert failed.failure_type is FailureType.NO_FEASIBLE_PLAN
    assert failed.failure_detail is not None and len(failed.failure_detail) == 1024
    assert "\x00" not in failed.failure_detail


async def test_concurrent_creates_with_one_key_make_one_row(store: JobStore) -> None:
    results = await asyncio.gather(*[_create(store, key="k-1") for _ in range(8)])
    ids = {job_id for job_id, _ in results}
    assert len(ids) == 1
    assert sum(created for _, created in results) == 1
    assert len(await store.list_jobs(limit=100)) == 1


async def test_same_key_other_request_is_a_conflict(store: JobStore) -> None:
    await _create(store, key="k-2", request_hash="a" * 64)
    with pytest.raises(IdempotencyConflictError):
        await _create(store, key="k-2", request_hash="b" * 64)
    replay_id, created = await _create(store, key="k-2", request_hash="a" * 64)
    assert not created
    assert len(await store.list_jobs(limit=100)) == 1
    assert (await store.get_job(replay_id)) is not None


async def test_fencing_tokens_are_allocated_1_2_3_even_concurrently(
    store: JobStore, node_id: str
) -> None:
    job_id, _ = await _create(store)
    attempts = await asyncio.gather(
        *[
            store.create_attempt(job_id, node_id, attempt_id=str(new_attempt_id()))
            for _ in range(5)
        ]
    )
    assert sorted(a.fencing_token for a in attempts) == [1, 2, 3, 4, 5]
    assert all(a.state is AttemptState.ASSIGNED for a in attempts)
    job = await store.get_job(job_id)
    assert job is not None and job.attempt_epoch == 5


async def test_attempt_cas_checks_state_and_fencing_token(
    store: JobStore, node_id: str
) -> None:
    job_id, _ = await _create(store)
    first = await store.create_attempt(
        job_id, node_id, attempt_id=str(new_attempt_id())
    )
    staged = await store.transition_attempt(
        first.id, AttemptEvent.STAGE, expected=AttemptState.ASSIGNED, fencing_token=1
    )
    assert staged.state is AttemptState.STAGING
    with pytest.raises(StaleFencingTokenError) as info:
        await store.transition_attempt(
            first.id,
            AttemptEvent.ENV_READY,
            expected=AttemptState.STAGING,
            fencing_token=2,
        )
    assert info.value.failure_type is FailureType.FENCED
    with pytest.raises(StaleStateError):  # duplicate report of the same arrow
        await store.transition_attempt(
            first.id,
            AttemptEvent.STAGE,
            expected=AttemptState.ASSIGNED,
            fencing_token=1,
        )
    await store.transition_attempt(
        first.id, AttemptEvent.ENV_READY, expected=AttemptState.STAGING, fencing_token=1
    )
    running = await store.transition_attempt(
        first.id, AttemptEvent.RUN, expected=AttemptState.ENV_READY, fencing_token=1
    )
    assert running.started_at is not None
    failed = await store.transition_attempt(
        first.id,
        AttemptEvent.FAIL,
        expected=AttemptState.RUNNING,
        fencing_token=1,
        failure_type=FailureType.USER_ERROR,
        exit_code=3,
        detail="exit 3",
    )
    assert failed.ended_at is not None and failed.exit_code == 3


async def test_phase_records_are_idempotent(store: JobStore, node_id: str) -> None:
    job_id, _ = await _create(store)
    attempt = await store.create_attempt(
        job_id, node_id, attempt_id=str(new_attempt_id())
    )
    at = dt.datetime(2026, 9, 28, tzinfo=dt.UTC)
    assert await store.record_phase(
        attempt.id, AttemptPhase.RUN, started_at=at, duration_ms=5
    )
    assert not await store.record_phase(
        attempt.id, AttemptPhase.RUN, started_at=at, duration_ms=5
    )
    phases = await store.phases(attempt.id)
    assert [(p.phase, p.duration_ms) for p in phases] == [("run", 5)]


async def test_list_jobs_newest_first_and_state_filter(store: JobStore) -> None:
    ids = [(await _create(store))[0] for _ in range(3)]
    await store.transition_job(ids[0], JobEvent.PLAN, expected=JobState.SUBMITTED)
    listed = await store.list_jobs(limit=10)
    assert [j.id for j in listed] == list(reversed(ids))
    assert [j.id for j in await store.list_jobs(limit=2)] == list(reversed(ids))[:2]
    planning = await store.list_jobs(limit=10, state=JobState.PLANNING)
    assert [j.id for j in planning] == [ids[0]]
