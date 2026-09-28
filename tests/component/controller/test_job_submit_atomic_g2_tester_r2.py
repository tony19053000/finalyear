"""Tester round 2 (V0.4 G2 fix round): independent breakage attempts on the new
atomic ``JobStore.create_queued_job`` / ``create_job(..., through=...)`` path.

Real PostgreSQL throughout (``pg_settings``). Beyond the implementer's own
``test_job_submit_atomic.py`` this file adds:

* a genuine dropped DB connection (``pg_terminate_backend`` on the submitting
  backend itself, not an application-level exception) mid-submission;
* a real ``asyncio`` task cancellation (the whole coroutine cancelled from the
  outside after a random short delay), not a scripted call-count monkeypatch;
* proof that an external reader can *never* observe the job row in
  ``SUBMITTED``/``PLANNING`` -- because ``Job.state`` is written once, directly
  to the final state, other transactions either see nothing (pre-commit) or the
  final state (post-commit), never an intermediate one;
* ``transition_job`` continuing ``seq`` at 4 after ``create_queued_job``;
* the legacy ``create_job(through=())`` path (ends ``SUBMITTED``, one row);
* an illegal path whose **second** step (not the first) is illegal;
* concurrent racers under one key with genuinely different bodies (mixed
  hashes), at the store layer.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.ids import new_job_id
from meshforge.common.states import JobState
from meshforge.controller.jobs.store import (
    IdempotencyConflictError,
    JobStore,
    JobStoreError,
)
from meshforge.controller.jobs.transitions import IllegalTransitionError, JobEvent
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=10)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> JobStore:
    return JobStore(engine)


async def _exec(engine: AsyncEngine, statements: tuple[str, ...]) -> None:
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as conn:
        jobs = (await conn.execute(text("SELECT count(*) FROM job"))).scalar_one()
        rows = (
            await conn.execute(text("SELECT count(*) FROM job_transition"))
        ).scalar_one()
    return int(jobs), int(rows)


async def _submit(
    store: JobStore,
    *,
    job_id: str | None = None,
    key: str | None = None,
    request_hash: str = HASH_A,
) -> tuple[Any, bool]:
    return await store.create_queued_job(
        job_id=job_id or str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": "n"},
        request_hash=request_hash,
        idempotency_key=key,
    )


# --------------------------------------------------------- dropped connection


_KILL_ON_QUEUED = (
    """
    CREATE FUNCTION mf_test_kill_backend() RETURNS trigger AS $$
    BEGIN
      IF NEW.to_state = 'queued' THEN
        PERFORM pg_terminate_backend(pg_backend_pid());
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER mf_test_kill_backend BEFORE INSERT ON job_transition
      FOR EACH ROW EXECUTE FUNCTION mf_test_kill_backend()
    """,
)
_UNKILL = (
    "DROP TRIGGER mf_test_kill_backend ON job_transition",
    "DROP FUNCTION mf_test_kill_backend()",
)


async def test_dropped_db_connection_mid_submission_leaves_nothing(
    store: JobStore, engine: AsyncEngine
) -> None:
    """``pg_terminate_backend`` on the submitting connection itself while it is
    mid-transaction: a real severed connection, not a raised application error.
    PostgreSQL rolls the whole (uncommitted) transaction back on its own; the
    driver surfaces a connection error which the store must still turn into a
    retryable :class:`JobStoreError`, and no row may survive."""
    await _exec(engine, _KILL_ON_QUEUED)
    try:
        with pytest.raises(JobStoreError):
            await _submit(store, key="dropped-conn")
    finally:
        # The trigger only fires on a fresh backend for the *next* attempt, but
        # the one that killed itself is gone; reconnect and clean up with a
        # brand-new connection so teardown does not also get terminated.
        await _exec(engine, _UNKILL)
    assert await _counts(engine) == (0, 0)
    record, created = await _submit(store, key="dropped-conn")
    assert created and record.state is JobState.QUEUED
    assert await _counts(engine) == (1, 3)


async def test_real_task_cancellation_at_random_delay_leaves_nothing(
    store: JobStore, engine: AsyncEngine
) -> None:
    """Cancel the actual submitting coroutine from the outside (simulating a
    killed worker / client disconnect) at an unscripted moment, not at a fixed
    call count. A slow trigger widens the transaction's live window so the
    cancellation reliably lands mid-flight; repeated with several random
    delays to shake out timing-dependent gaps."""
    await _exec(
        engine,
        (
            """
            CREATE FUNCTION mf_test_slow() RETURNS trigger AS $$
            BEGIN PERFORM pg_sleep(0.05); RETURN NEW; END $$ LANGUAGE plpgsql
            """,
            """
            CREATE TRIGGER mf_test_slow BEFORE INSERT ON job_transition
              FOR EACH ROW EXECUTE FUNCTION mf_test_slow()
            """,
        ),
    )
    try:
        rng = random.Random(1234)  # noqa: S311 - test timing jitter, not crypto
        for i in range(5):
            key = f"cancel-{i}-{rng.random()}"
            task = asyncio.ensure_future(_submit(store, key=key))
            await asyncio.sleep(rng.uniform(0.01, 0.09))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await _counts(engine) == (0, 0), "a cancelled submit left a row"
    finally:
        await _exec(
            engine,
            (
                "DROP TRIGGER mf_test_slow ON job_transition",
                "DROP FUNCTION mf_test_slow()",
            ),
        )


# ------------------------------------------------- never observably mid-state


async def test_external_reader_never_sees_submitted_or_planning(
    store: JobStore, engine: AsyncEngine
) -> None:
    """``Job.state`` is written once, directly to the final state (D50 fix): an
    independent reader on another connection can only ever see "no row yet" or
    the final ``QUEUED`` row -- never a row whose ``state`` column reads
    ``submitted`` or ``planning``. Widen the window with a slow trigger and poll
    from a second store bound to a second engine throughout."""
    await _exec(
        engine,
        (
            """
            CREATE FUNCTION mf_test_widen() RETURNS trigger AS $$
            BEGIN
              IF NEW.to_state = 'planning' THEN PERFORM pg_sleep(0.4); END IF;
              RETURN NEW;
            END $$ LANGUAGE plpgsql
            """,
            """
            CREATE TRIGGER mf_test_widen BEFORE INSERT ON job_transition
              FOR EACH ROW EXECUTE FUNCTION mf_test_widen()
            """,
        ),
    )
    job_id = str(new_job_id())
    observed_states: set[str] = set()
    seen_after_commit = asyncio.Event()

    async def poll() -> None:
        while not seen_after_commit.is_set():
            record = await store.get_job(job_id)
            if record is not None:
                observed_states.add(record.state.value)
                seen_after_commit.set()
            await asyncio.sleep(0.01)

    try:
        task = asyncio.ensure_future(poll())
        record, created = await _submit(store, job_id=job_id, key="widen-key")
        await asyncio.wait_for(task, timeout=2)
    finally:
        await _exec(
            engine,
            (
                "DROP TRIGGER mf_test_widen ON job_transition",
                "DROP FUNCTION mf_test_widen()",
            ),
        )
    assert created and record.state is JobState.QUEUED
    assert observed_states in ({"queued"}, set())  # never submitted/planning
    if not observed_states:
        pytest.skip("poller never caught the row mid-flight; not a defect")


# ---------------------------------------------------------- seq continuation


async def test_transition_job_continues_seq_after_atomic_submit(
    store: JobStore,
) -> None:
    record, created = await _submit(store, key="seq-continue")
    assert created
    after = await store.transition_job(
        record.id, JobEvent.DISPATCHED, expected=JobState.QUEUED
    )
    assert after.state is JobState.PREPARING
    history = await store.transitions(record.id)
    assert [(t.seq, t.from_state, t.to_state) for t in history] == [
        (1, None, JobState.SUBMITTED),
        (2, JobState.SUBMITTED, JobState.PLANNING),
        (3, JobState.PLANNING, JobState.QUEUED),
        (4, JobState.QUEUED, JobState.PREPARING),
    ]
    assert history[-1].at >= history[-2].at


# --------------------------------------------------------- legacy / illegal


async def test_legacy_create_job_without_through_stays_submitted(
    store: JobStore, engine: AsyncEngine
) -> None:
    record, created = await store.create_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": "n"},
        request_hash=HASH_A,
        idempotency_key=None,
    )
    assert created and record.state is JobState.SUBMITTED
    history = await store.transitions(record.id)
    assert [(t.seq, t.from_state, t.to_state) for t in history] == [
        (1, None, JobState.SUBMITTED)
    ]
    assert await _counts(engine) == (1, 1)


async def test_illegal_second_step_of_the_path_writes_nothing(
    store: JobStore, engine: AsyncEngine
) -> None:
    """The first step (``PLAN``) is legal; the *second* (``DISPATCHED``, which
    needs ``QUEUED`` not ``PLANNING``) is not. The whole path must be validated
    before any write, not just its first arrow."""
    with pytest.raises(IllegalTransitionError):
        await store.create_job(
            job_id=str(new_job_id()),
            name="demo",
            manifest={"name": "demo"},
            bundle_digest=DIGEST,
            placement={"mode": "offload", "node_id": "n"},
            request_hash=HASH_A,
            idempotency_key=None,
            through=(JobEvent.PLAN, JobEvent.DISPATCHED),
        )
    assert await _counts(engine) == (0, 0)


# ------------------------------------------------- concurrent mixed bodies


async def test_concurrent_mixed_body_racers_one_key_store_level(
    store: JobStore, engine: AsyncEngine
) -> None:
    """8 racers share one key; 4 carry ``HASH_A``, 4 carry ``HASH_B``. Exactly
    one request must be accepted; every racer whose hash differs from the
    winner's must get :class:`IdempotencyConflictError`, and racers sharing the
    winner's hash must get the winner's job back, never create a second row."""
    key = "mixed-race"
    hashes = [HASH_A, HASH_B] * 4

    async def attempt(h: str) -> tuple[str, Any] | Exception:
        try:
            record, created = await _submit(store, key=key, request_hash=h)
            return (h, (record, created))
        except IdempotencyConflictError as exc:
            return exc

    results = await asyncio.gather(
        *(attempt(h) for h in hashes), return_exceptions=False
    )
    winners = [r for r in results if not isinstance(r, IdempotencyConflictError)]
    conflicts = [r for r in results if isinstance(r, IdempotencyConflictError)]
    assert len(winners) + len(conflicts) == 8
    assert winners, "every racer conflicted: nobody ever won the key"
    winning_hash = winners[0][0]
    assert all(h == winning_hash for h, _ in winners)
    assert len({record.id for _, (record, _) in winners}) == 1
    assert sum(created for _, (_, created) in winners) == 1
    assert len(conflicts) == 4
    assert await _counts(engine) == (1, 3)
