"""V0.8 G1 fix round 1 (D182 T-2/T-3): change-feed notes follow their
savepoint frame - released savepoints hand notes to the enclosing frame, rolled
back ones drop only their own, only the outermost commit publishes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.events import changes
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


class _BoomError(Exception):
    pass


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    made = create_engine(pg_settings, pool_size=2)
    try:
        yield made
    finally:
        changes.detach(made)
        await made.dispose()


def _ids(seen: list[changes.Change]) -> list[str]:
    return [c.data["job_id"] for c in seen]


async def test_nested_savepoints_keep_order_and_drop_only_the_rolled_back_frame(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        changes.note_job(db, "a", "queued", owner_user_id=None)
        async with db.begin_nested():
            changes.note_job(db, "b", "queued", owner_user_id=None)
            async with db.begin_nested():  # released into the middle frame
                changes.note_job(db, "c", "queued", owner_user_id=None)
            try:
                async with db.begin_nested():  # rolled back: only "d" goes
                    changes.note_job(db, "d", "queued", owner_user_id=None)
                    raise _BoomError
            except _BoomError:
                pass
            changes.note_job(db, "e", "queued", owner_user_id=None)
            assert seen == [], "nothing is published before the outer commit"
        changes.note_job(db, "f", "queued", owner_user_id=None)
        assert seen == []
    assert _ids(seen) == ["a", "b", "c", "e", "f"]


async def test_a_rolled_back_middle_frame_drops_what_its_inner_frames_released(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        changes.note_job(db, "kept", "queued", owner_user_id=None)
        try:
            async with db.begin_nested():
                async with db.begin_nested():
                    changes.note_job(db, "inner", "queued", owner_user_id=None)
                changes.note_job(db, "middle", "queued", owner_user_id=None)
                raise _BoomError
        except _BoomError:
            pass
    assert _ids(seen) == ["kept"]


async def test_a_session_closed_without_commit_leaks_nothing_into_its_next_transaction(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    db = sessions()
    await db.execute(text("SELECT 1"))
    changes.note_job(db, "abandoned", "queued", owner_user_id=None)
    await db.close()  # no commit, no explicit rollback
    changes.note_job(db, "next", "queued", owner_user_id=None)
    await db.commit()
    await db.close()
    assert _ids(seen) == ["next"]


async def test_the_outer_commit_with_an_open_savepoint_publishes_its_notes_once(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    async with sessions() as db:
        changes.note_job(db, "outer", "queued", owner_user_id=None)
        await db.begin_nested()
        changes.note_job(db, "open", "queued", owner_user_id=None)
        await db.commit()  # commits the savepoint and then the transaction
    assert _ids(seen) == ["outer", "open"]
