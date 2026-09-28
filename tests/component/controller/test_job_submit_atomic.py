"""Atomic job submission against real PostgreSQL (V0.4 G2 fix round, D49/D50).

``JobStore.create_queued_job`` must write the job row and its whole
``SUBMITTED -> PLANNING -> QUEUED`` history in ONE transaction. The failure tests
break the submission *after* the job INSERT - with a real PostgreSQL trigger that
raises on the last history row, and with a cancellation injected at the same
point - and assert that nothing at all survives. If the transaction were split
(e.g. INSERT + SUBMITTED committed first), these tests would find a stranded
SUBMITTED/PLANNING row.
"""

from __future__ import annotations

import asyncio
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
from meshforge.controller.jobs import store as store_module
from meshforge.controller.jobs.store import JobStore, JobStoreError
from meshforge.controller.jobs.transitions import IllegalTransitionError, JobEvent
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "cd" * 32
HASH = "a" * 64

# Raises inside PostgreSQL when the QUEUED history row (the last write of a
# submission) is inserted: a genuine database error after the job INSERT.
_BREAK_QUEUED_ROW = (
    """
    CREATE FUNCTION mf_test_break_queued() RETURNS trigger AS $$
    BEGIN
      IF NEW.to_state = 'queued' THEN
        RAISE EXCEPTION 'injected failure after the job insert';
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER mf_test_break_queued BEFORE INSERT ON job_transition
      FOR EACH ROW EXECUTE FUNCTION mf_test_break_queued()
    """,
)
_UNBREAK = (
    "DROP TRIGGER mf_test_break_queued ON job_transition",
    "DROP FUNCTION mf_test_break_queued()",
)


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
    store: JobStore, *, key: str | None = None, request_hash: str = HASH
) -> tuple[Any, bool]:
    return await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": "n"},
        request_hash=request_hash,
        idempotency_key=key,
    )


async def test_one_call_gives_queued_with_three_timed_history_rows(
    store: JobStore,
) -> None:
    record, created = await _submit(store)
    assert created and record.state is JobState.QUEUED
    stored = await store.get_job(record.id)
    assert stored is not None and stored.state is JobState.QUEUED
    history = await store.transitions(record.id)
    assert [(t.seq, t.from_state, t.to_state) for t in history] == [
        (1, None, JobState.SUBMITTED),
        (2, JobState.SUBMITTED, JobState.PLANNING),
        (3, JobState.PLANNING, JobState.QUEUED),
    ]
    stamps = [t.at for t in history]
    assert all(at.tzinfo is not None for at in stamps)
    assert stamps == sorted(stamps)  # non-decreasing
    assert record.created_at <= stamps[0]
    # clock_timestamp(), not the transaction's now(): the rows carry their own time.
    assert stamps[0] < stamps[2]


@pytest.mark.parametrize("key", [None, "atomic-key"])
async def test_database_error_after_insert_leaves_nothing(
    store: JobStore, engine: AsyncEngine, key: str | None
) -> None:
    await _exec(engine, _BREAK_QUEUED_ROW)
    with pytest.raises(JobStoreError):
        await _submit(store, key=key)
    assert await _counts(engine) == (0, 0)
    # The key is not burned: once the database is healthy the retry succeeds.
    await _exec(engine, _UNBREAK)
    record, created = await _submit(store, key=key)
    assert created and record.state is JobState.QUEUED
    assert await _counts(engine) == (1, 3)


async def test_cancellation_after_insert_leaves_nothing(
    store: JobStore, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request cancelled (client gone, shutdown) mid-submission rolls back."""
    real = store_module._append_transition
    calls = 0

    async def cancel_on_last_row(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise asyncio.CancelledError
        await real(*args, **kwargs)

    monkeypatch.setattr(store_module, "_append_transition", cancel_on_last_row)
    with pytest.raises(asyncio.CancelledError):
        await _submit(store, key="cancel-key")
    assert calls == 3  # the job INSERT and two history rows had been written
    assert await _counts(engine) == (0, 0)


async def test_illegal_path_is_refused_before_any_write(
    store: JobStore, engine: AsyncEngine
) -> None:
    with pytest.raises(IllegalTransitionError):
        await store.create_job(
            job_id=str(new_job_id()),
            name="demo",
            manifest={"name": "demo"},
            bundle_digest=DIGEST,
            placement={"mode": "offload", "node_id": "n"},
            request_hash=HASH,
            idempotency_key=None,
            through=(JobEvent.PLANNED,),  # SUBMITTED has no PLANNED arrow
        )
    assert await _counts(engine) == (0, 0)


async def test_concurrent_same_key_submissions_all_see_queued(
    store: JobStore, engine: AsyncEngine
) -> None:
    results = await asyncio.gather(*[_submit(store, key="race") for _ in range(8)])
    assert sum(created for _, created in results) == 1
    assert len({record.id for record, _ in results}) == 1
    assert all(record.state is JobState.QUEUED for record, _ in results)
    assert await _counts(engine) == (1, 3)
