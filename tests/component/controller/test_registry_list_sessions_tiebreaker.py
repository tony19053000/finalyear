"""V0.3 G5 reviewer fix round N3: ``list_sessions(limit=...)`` needs a tiebreaker.

``ORDER BY started_at DESC LIMIT n`` alone leaves it to PostgreSQL which of two
sessions sharing a ``started_at`` falls inside the window - so
``GET /v1/sessions?limit=2`` could return a different pair on different calls, and
the pair it returns need not be the tail of the unlimited list. Two sessions *can*
share the timestamp: ``started_at`` is one ``datetime.now`` per call into a
``timestamptz`` column, and the source clock is coarse on Windows.

Real PostgreSQL, because this is an ordering guarantee of the SQL and not of
Python: the rows are given identical timestamps with one ``UPDATE``, then the
window is read repeatedly. ``session_id`` is a ULID, so its lexical order is also
creation order - which is what makes "the newest two" predictable here.
"""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

SHARED_TIMESTAMP = dt.datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=dt.UTC)


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
    """The tables, without starting a controller: this module only needs SQL.

    Synchronous on purpose - ``upgrade_to_head`` drives Alembic's own event loop,
    which is why the runtime calls it through ``asyncio.to_thread``.
    """
    upgrade_to_head(pg_settings)
    return pg_settings


@pytest_asyncio.fixture
async def store(migrated: DatabaseSettings) -> AsyncIterator[Registry]:
    engine = create_engine(migrated)
    try:
        yield Registry(engine)
    finally:
        await engine.dispose()


async def _node_with_sessions(store: Registry, count: int) -> tuple[str, list[str]]:
    """One node with ``count`` sessions, returned in creation order."""
    node_id = str(new_node_id())
    session_ids: list[str] = []
    for epoch in range(1, count + 1):
        session_id = str(new_session_id())
        session_ids.append(session_id)
        await store.upsert_node_on_hello(
            node_id=node_id,
            hostname="component-test-laptop",
            os="TestOS 1.0",
            platform="test-x86_64",
            hardware={"cpu_model": "test-cpu"},
            session_epoch=epoch,
            session_id=session_id,
            state=NodeState.ENROLLING,
        )
        await store.open_session(
            session_id=session_id,
            node_id=node_id,
            session_epoch=epoch,
            peer_address="ipv4:127.0.0.1:1",
            agent_version="0.1.0.dev0",
            protocol_version=1,
        )
    return node_id, session_ids


async def _set_started_at(
    settings: DatabaseSettings, values: dict[str, dt.datetime]
) -> None:
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            for session_id, when in values.items():
                await connection.execute(
                    text(
                        "UPDATE node_session SET started_at = :when "
                        "WHERE session_id = :sid"
                    ),
                    {"when": when, "sid": session_id},
                )
    finally:
        await engine.dispose()


async def test_the_limited_window_is_deterministic_for_equal_timestamps(
    store: Registry, migrated: DatabaseSettings
) -> None:
    node_id, session_ids = await _node_with_sessions(store, 3)
    await _set_started_at(migrated, dict.fromkeys(session_ids, SHARED_TIMESTAMP))

    expected = sorted(sorted(session_ids, reverse=True)[:2])  # newest two, oldest first

    for _ in range(5):
        rows = await store.list_sessions(node_id, limit=2)
        assert [row.session_id for row in rows] == expected


async def test_the_window_is_a_suffix_of_the_full_list(
    store: Registry, migrated: DatabaseSettings
) -> None:
    """With ties, ``limit`` must still mean "the last n of the same ordering"."""
    node_id, session_ids = await _node_with_sessions(store, 4)
    await _set_started_at(migrated, dict.fromkeys(session_ids, SHARED_TIMESTAMP))

    everything = [row.session_id for row in await store.list_sessions(node_id)]
    assert len(everything) == 4
    for limit in (1, 2, 3, 4):
        window = [
            row.session_id for row in await store.list_sessions(node_id, limit=limit)
        ]
        assert window == everything[-limit:]


async def test_started_at_still_wins_over_the_tiebreaker(
    store: Registry, migrated: DatabaseSettings
) -> None:
    """The tiebreaker must stay secondary: here the *oldest* row has the highest
    ``session_id``, so an ordering that sorted by ``session_id`` first would
    return the list reversed."""
    node_id, session_ids = await _node_with_sessions(store, 3)
    descending_ids = sorted(session_ids, reverse=True)
    await _set_started_at(
        migrated,
        {
            session_id: SHARED_TIMESTAMP + dt.timedelta(minutes=offset)
            for offset, session_id in enumerate(descending_ids)
        },
    )

    assert [
        row.session_id for row in await store.list_sessions(node_id)
    ] == descending_ids
    assert [
        row.session_id for row in await store.list_sessions(node_id, limit=2)
    ] == descending_ids[-2:]
