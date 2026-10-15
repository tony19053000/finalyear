"""V0.8 G2 item 0 tester r1: T-5 deviation (D184) - a note made with no open
transaction begins the root transaction. Attack: rollback/close/commit,
savepoints, session reuse, raising sink, begin() side effect, concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.events import changes
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    made = create_engine(pg_settings, pool_size=8)
    try:
        yield made
    finally:
        changes.detach(made)
        await made.dispose()


def _n(db, name: str) -> None:
    changes.note_job(db, name, "queued", owner_user_id=None)


def _ids(seen: list[changes.Change]) -> list[str]:
    return [c.data["job_id"] for c in seen]


async def test_orphan_note_then_rollback_is_dropped(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "orphan")
        assert db.in_transaction()
        await db.rollback()
        await db.commit()  # empty commit afterwards publishes nothing
    assert seen == []


async def test_orphan_note_then_close_is_dropped_and_session_reusable(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    db = create_session_factory(engine)()
    _n(db, "orphan")
    await db.close()
    await db.execute(text("select 1"))
    await db.commit()
    assert seen == []


async def test_orphan_note_then_commit_publishes_exactly_once(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "a")
        _n(db, "b")
        await db.commit()
        await db.commit()  # second commit must not republish
    assert _ids(seen) == ["a", "b"]


async def test_orphan_rolled_back_does_not_leak_into_next_commit(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "gone")
        await db.rollback()
        _n(db, "kept")
        await db.commit()
        _n(db, "gone2")
        await db.rollback()
        await db.execute(text("select 1"))
        await db.commit()
    assert _ids(seen) == ["kept"]


async def test_orphan_note_survives_a_following_statement_and_commits(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "a")
        await db.execute(text("select 1"))  # autobegin must be a no-op now
        _n(db, "b")
        await db.commit()
    assert _ids(seen) == ["a", "b"]


async def test_orphan_note_then_savepoint_rollback_keeps_the_orphan(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "orphan")
        sp = await db.begin_nested()
        _n(db, "inner")
        await sp.rollback()
        await db.commit()
    assert _ids(seen) == ["orphan"]


async def test_orphan_note_then_savepoint_release_then_outer_rollback(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "orphan")
        async with db.begin_nested():
            _n(db, "inner")
        assert seen == []
        await db.rollback()
        await db.commit()
    assert seen == []


async def test_orphan_note_then_savepoint_release_then_commit(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "orphan")
        async with db.begin_nested():
            _n(db, "inner")
        await db.commit()
    assert sorted(_ids(seen)) == ["inner", "orphan"]
    assert len(seen) == 2


async def test_first_note_inside_a_savepoint_opened_with_no_transaction(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        async with db.begin_nested():  # autobegins the root
            _n(db, "inner")
        _n(db, "after")
        await db.rollback()
    assert seen == []


async def test_no_sink_means_no_transaction_is_begun(engine) -> None:
    async with create_session_factory(engine)() as db:
        _n(db, "x")
        assert not db.in_transaction()  # unattached engine: zero cost, no side effect


async def test_note_after_commit_belongs_to_the_new_transaction(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "one")
        await db.commit()
        _n(db, "two")
        await db.rollback()
        _n(db, "three")
        await db.commit()
    assert _ids(seen) == ["one", "three"]


async def test_raising_sink_does_not_fail_an_orphan_commit(engine) -> None:
    def boom(_: list[changes.Change]) -> None:
        raise RuntimeError("sink down")

    changes.attach(engine, boom)
    async with create_session_factory(engine)() as db:
        _n(db, "a")
        await db.commit()
        _n(db, "b")
        await db.rollback()


async def test_begin_after_an_orphan_note_raises_already_begun(engine) -> None:
    """Documents the accepted side effect: same as after any autobegun statement."""
    changes.attach(engine, lambda _: None)
    async with create_session_factory(engine)() as db:
        _n(db, "a")
        with pytest.raises(InvalidRequestError, match="already begun"):
            await db.begin()
        await db.rollback()


async def test_begin_before_note_is_unchanged(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        async with db.begin():
            _n(db, "a")
        async with db.begin():
            _n(db, "b")
            await db.rollback()
    assert _ids(seen) == ["a"]


async def test_session_factory_begin_style_still_publishes_once(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine).begin() as db:
        _n(db, "a")
    assert _ids(seen) == ["a"]


async def test_exception_inside_context_with_orphan_note_publishes_nothing(
    engine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    with pytest.raises(ValueError):  # noqa: PT012
        async with create_session_factory(engine)() as db:
            _n(db, "a")
            raise ValueError
    assert seen == []


async def test_the_per_transaction_cap_still_holds_for_orphans(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        for i in range(10_050):
            _n(db, f"n{i}")
        await db.commit()
    assert len(seen) == 10_000


async def test_concurrent_orphan_sessions_never_cross_publish(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    factory = create_session_factory(engine)

    async def worker(i: int) -> None:
        async with factory() as db:
            _n(db, f"s{i}")
            await asyncio.sleep(0)
            _n(db, f"s{i}b")
            if i % 2:
                await db.rollback()
            else:
                await db.commit()

    await asyncio.gather(*(worker(i) for i in range(40)))
    got = sorted(_ids(seen))
    want = sorted(f"s{i}{s}" for i in range(0, 40, 2) for s in ("", "b"))
    assert got == want
