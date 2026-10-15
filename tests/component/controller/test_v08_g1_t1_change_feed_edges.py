"""V0.8 G1 tester r1: the change feed (D172) "events only after commit" under
savepoints, explicit rollbacks, a failing sink and concurrent sessions."""

# ruff: noqa: PT012

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.events import changes
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    made = create_engine(pg_settings, pool_size=5)
    try:
        yield made
    finally:
        changes.detach(made)
        await made.dispose()


def _ids(seen: list[changes.Change]) -> list[str]:
    return [c.data["job_id"] for c in seen]


async def test_t2_a_rolled_back_savepoint_publishes_nothing(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)

    class BoomError(Exception):
        pass

    async with sessions() as db, db.begin():
        changes.note_job(db, "job_kept", "queued", owner_user_id=None)
        try:
            async with db.begin_nested():
                changes.note_job(db, "job_undone", "running", owner_user_id=None)
                raise BoomError
        except BoomError:
            pass
    assert _ids(seen) == ["job_kept"]


async def test_t3_a_released_savepoint_does_not_publish_before_the_outer_commit(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        async with sessions() as db, db.begin():
            changes.note_job(db, "job_a", "queued", owner_user_id=None)
            async with db.begin_nested():
                changes.note_job(db, "job_b", "queued", owner_user_id=None)
            raise BoomError  # the outer transaction rolls back
    assert seen == [], "events were published for a transaction that rolled back"


async def test_a_savepoint_inside_a_committing_transaction_publishes_everything_once(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        changes.note_job(db, "job_a", "queued", owner_user_id=None)
        async with db.begin_nested():
            changes.note_job(db, "job_b", "queued", owner_user_id=None)
        changes.note_job(db, "job_c", "queued", owner_user_id=None)
    assert sorted(_ids(seen)) == ["job_a", "job_b", "job_c"]
    assert len(_ids(seen)) == 3, "no duplicates"


async def test_an_explicit_rollback_then_a_fresh_transaction_leaks_nothing(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    async with sessions() as db:
        await db.execute(text("SELECT 1"))
        changes.note_job(db, "job_old", "queued", owner_user_id=None)
        await db.rollback()
        changes.note_job(db, "job_new", "queued", owner_user_id=None)
        await db.commit()
    assert _ids(seen) == ["job_new"]


async def test_an_integrity_error_at_flush_publishes_nothing(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    with pytest.raises(IntegrityError):
        async with sessions() as db, db.begin():
            changes.note_job(db, "job_x", "queued", owner_user_id=None)
            await db.execute(
                text("INSERT INTO api_token (id) VALUES ('definitely-incomplete')")
            )
    assert seen == []


async def test_a_raising_sink_never_fails_the_commit_and_later_commits_still_publish(
    engine: AsyncEngine,
) -> None:
    calls: list[int] = []

    def bad(batch: list[changes.Change]) -> None:
        calls.append(len(batch))
        raise RuntimeError("sink exploded")

    changes.attach(engine, bad)
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        changes.note_job(db, "job_1", "queued", owner_user_id=None)
    async with sessions() as db, db.begin():
        changes.note_job(db, "job_2", "queued", owner_user_id=None)
    assert calls == [1, 1]


async def test_concurrent_sessions_publish_only_their_own_notes(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    gate = asyncio.Event()

    async def commits() -> None:
        async with sessions() as db, db.begin():
            changes.note_job(db, "job_commits", "queued", owner_user_id="u1")
            gate.set()
            await asyncio.sleep(0.05)

    async def rolls_back() -> None:
        await gate.wait()
        try:
            async with sessions() as db, db.begin():
                changes.note_job(db, "job_rolls", "queued", owner_user_id="u2")
                raise ValueError
        except ValueError:
            pass

    await asyncio.gather(commits(), rolls_back())
    assert _ids(seen) == ["job_commits"] and seen[0].owner_user_id == "u1"


async def test_a_huge_transaction_is_capped_not_unbounded(engine: AsyncEngine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        for i in range(12_000):
            changes.note_job(db, f"job_{i}", "queued", owner_user_id=None)
    assert len(seen) == 10_000
