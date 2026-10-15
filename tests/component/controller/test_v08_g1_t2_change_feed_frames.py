"""V0.8 G1 tester r2: change-feed savepoint frames (D182 T-2/T-3), attacked
deeper: 4-deep nesting, release-then-rollback of the parent, a raising sink,
session reuse after rollback/close, explicit commit() style, concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
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
    made = create_engine(pg_settings, pool_size=6)
    try:
        yield made
    finally:
        changes.detach(made)
        await made.dispose()


def _ids(seen: list[changes.Change]) -> list[str]:
    return [c.data["job_id"] for c in seen]


def _n(db, name: str) -> None:
    changes.note_job(db, name, "queued", owner_user_id=None)


async def test_four_deep_all_released_publish_once_at_outer_commit(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db, db.begin():
        _n(db, "l0")
        async with db.begin_nested():
            _n(db, "l1")
            async with db.begin_nested():
                _n(db, "l2")
                async with db.begin_nested():
                    _n(db, "l3")
                    async with db.begin_nested():
                        _n(db, "l4")
                _n(db, "l2b")
            _n(db, "l1b")
        assert seen == []
    assert sorted(_ids(seen)) == sorted(["l0", "l1", "l2", "l3", "l4", "l2b", "l1b"])


async def test_deepest_rollback_in_a_four_deep_chain_drops_only_itself(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db, db.begin(), db.begin_nested():
        _n(db, "l1")
        async with db.begin_nested():
            _n(db, "l2")
            async with db.begin_nested():
                _n(db, "l3")
                try:
                    async with db.begin_nested():
                        _n(db, "l4")
                        raise _BoomError
                except _BoomError:
                    pass
                _n(db, "l3b")
    assert sorted(_ids(seen)) == ["l1", "l2", "l3", "l3b"]


async def test_release_then_parent_rolls_back_drops_released_child(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db, db.begin():
        _n(db, "top")
        try:
            async with db.begin_nested():
                _n(db, "parent")
                async with db.begin_nested():
                    _n(db, "child")
                async with db.begin_nested(), db.begin_nested():
                    _n(db, "grandchild")
                raise _BoomError
        except _BoomError:
            pass
    assert _ids(seen) == ["top"]


async def test_outer_rollback_after_released_savepoints_publishes_nothing(
    engine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        async with db.begin():
            pass
        await db.begin()
        _n(db, "x")
        async with db.begin_nested():
            _n(db, "y")
        await db.rollback()
        assert seen == []
        # reuse after rollback: next transaction publishes only its own
        async with db.begin():
            _n(db, "z")
    assert _ids(seen) == ["z"]


async def test_explicit_commit_style_and_reuse(engine) -> None:
    from sqlalchemy import text

    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        await db.execute(text("select 1"))  # a store writes before it notes
        _n(db, "a")
        sp = await db.begin_nested()
        _n(db, "b")
        await sp.commit()
        assert seen == []
        await db.commit()
        assert _ids(seen) == ["a", "b"]
        await db.execute(text("select 1"))
        _n(db, "c")
        await db.rollback()
        await db.execute(text("select 1"))
        _n(db, "d")
        await db.commit()
    assert _ids(seen) == ["a", "b", "d"]


async def test_t5_note_without_an_open_transaction_does_not_leak_after_rollback(
    engine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "orphan")
        await db.rollback()
        async with db.begin():
            _n(db, "real")
    assert _ids(seen) == ["real"]


async def test_t5_note_without_an_open_transaction_dies_with_close(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    db = create_session_factory(engine)()
    _n(db, "orphan")
    await db.close()
    async with db.begin():
        _n(db, "real")
    await db.close()
    assert _ids(seen) == ["real"]


async def test_t5_note_without_an_open_transaction_publishes_on_commit(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db:
        _n(db, "first")
        assert db.in_transaction()  # the note opened the root transaction
        await db.commit()
    assert _ids(seen) == ["first"]


async def test_savepoint_open_then_session_closed_leaks_nothing(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)
    db = sessions()
    await db.begin()
    _n(db, "a")
    await db.begin_nested()
    _n(db, "b")
    await db.close()
    assert seen == []
    async with db.begin():
        _n(db, "next")
    assert _ids(seen) == ["next"]
    await db.close()


async def test_raising_sink_does_not_fail_commit_or_lose_later_publishes(
    engine,
) -> None:
    calls: list[list[str]] = []

    def sink(batch: list[changes.Change]) -> None:
        calls.append(_ids(batch))
        if len(calls) == 1:
            raise RuntimeError("sink down")

    changes.attach(engine, sink)
    async with create_session_factory(engine)() as db:
        async with db.begin(), db.begin_nested():
            _n(db, "a")
        async with db.begin():
            _n(db, "b")
    assert calls == [["a"], ["b"]]


async def test_concurrent_sessions_do_not_mix_notes(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)

    async def worker(i: int, fail: bool) -> None:
        async with sessions() as db:
            try:
                async with db.begin():
                    _n(db, f"w{i}-top")
                    async with db.begin_nested():
                        _n(db, f"w{i}-sp")
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.01)
                    if fail:
                        raise _BoomError
            except _BoomError:
                pass

    await asyncio.gather(*(worker(i, i % 2 == 1) for i in range(12)))
    expect = sorted(f"w{i}-{k}" for i in range(0, 12, 2) for k in ("top", "sp"))
    assert sorted(_ids(seen)) == expect


async def test_notes_cap_survives_frame_bookkeeping(engine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    async with create_session_factory(engine)() as db, db.begin(), db.begin_nested():
        for i in range(10_050):
            _n(db, f"n{i}")
    assert len(seen) == 10_000
