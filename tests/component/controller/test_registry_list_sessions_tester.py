"""Independent tester coverage for V0.3 G5 fix round N3, real PostgreSQL.

Complements the implementer's own
``test_registry_list_sessions_tiebreaker.py`` (which is thorough on the tied
case) with the boundary shapes the fix-round brief calls out that file does not
cover: ``limit=1``, ``limit`` larger than the row count, and whether the
**no-limit** path is really unaffected by N3.

On that last point: it is not "byte for byte" unchanged. The diff
(``git diff 32d588f..bb8dd85`` on ``registry.py``) shows the no-limit branch
gained ``NodeSession.session_id`` as a *secondary* sort key too
(``order_by(NodeSession.started_at, NodeSession.session_id)`` vs. the old
``order_by(NodeSession.started_at)`` alone). For sessions with distinct
``started_at`` this changes nothing observable; for sessions that tie, the old
query left the order to PostgreSQL (which, absent a defined tiebreaker, is
permitted to return either order and is not required to be stable across
identical queries), so "unchanged" is stronger than what the old code actually
guaranteed - this file pins the new, better, guarantee instead of insisting on
literal identity with an unspecified old behaviour.
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

SHARED_TIMESTAMP = dt.datetime(2026, 5, 1, 0, 0, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
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
    node_id = str(new_node_id())
    session_ids: list[str] = []
    for epoch in range(1, count + 1):
        session_id = str(new_session_id())
        session_ids.append(session_id)
        await store.upsert_node_on_hello(
            node_id=node_id,
            hostname="tester-laptop",
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


# ----------------------------------------------------------------- limit=1


async def test_limit_one_returns_exactly_the_single_newest_session(
    store: Registry, migrated: DatabaseSettings
) -> None:
    node_id, session_ids = await _node_with_sessions(store, 5)
    await _set_started_at(
        migrated,
        {
            sid: SHARED_TIMESTAMP + dt.timedelta(minutes=i)
            for i, sid in enumerate(session_ids)
        },
    )
    rows = await store.list_sessions(node_id, limit=1)
    assert [r.session_id for r in rows] == [session_ids[-1]]


async def test_limit_one_with_a_tied_timestamp_is_still_deterministic(
    store: Registry, migrated: DatabaseSettings
) -> None:
    node_id, session_ids = await _node_with_sessions(store, 3)
    await _set_started_at(migrated, dict.fromkeys(session_ids, SHARED_TIMESTAMP))
    expected = [max(session_ids)]
    for _ in range(5):
        rows = await store.list_sessions(node_id, limit=1)
        assert [r.session_id for r in rows] == expected


# --------------------------------------------------- limit larger than count


async def test_limit_larger_than_the_row_count_returns_everything(
    store: Registry, migrated: DatabaseSettings
) -> None:
    node_id, session_ids = await _node_with_sessions(store, 3)
    rows = await store.list_sessions(node_id, limit=500)
    assert [r.session_id for r in rows] == session_ids  # oldest first, all 3
    assert len(rows) == 3


async def test_limit_larger_than_count_with_ties_still_returns_every_row_once(
    store: Registry, migrated: DatabaseSettings
) -> None:
    node_id, session_ids = await _node_with_sessions(store, 4)
    await _set_started_at(migrated, dict.fromkeys(session_ids, SHARED_TIMESTAMP))
    rows = await store.list_sessions(node_id, limit=500)
    assert sorted(r.session_id for r in rows) == sorted(session_ids)
    assert len(rows) == 4  # no duplication, no silent drop


# ------------------------------------------------- the no-limit path, N3's edge


async def test_the_no_limit_path_is_deterministic_for_tied_timestamps_too(
    store: Registry, migrated: DatabaseSettings
) -> None:
    """N3's diff added ``session_id`` as a secondary sort key to the *unlimited*
    query too, not only the ``limit``-bounded one; confirm the resulting order is
    the documented one and is stable across repeated calls.

    **This test is documentation, not proof that the tiebreaker is needed here.**
    Review round 2 mutated the secondary key away and this test stayed green:
    PostgreSQL's heap-scan order for a freshly-inserted, never-updated small
    table happens to match insertion order, which for ULIDs is ascending
    ``session_id``. The mutation-killing test for N3 is
    ``test_registry_list_sessions_tiebreaker.py::test_the_limited_window_is_deterministic_for_equal_timestamps``,
    which forces real disagreement via the ``limit``-bounded path. Kept because
    the invariant it asserts is correct and cheap to check.
    """
    node_id, session_ids = await _node_with_sessions(store, 4)
    await _set_started_at(migrated, dict.fromkeys(session_ids, SHARED_TIMESTAMP))
    expected = sorted(session_ids)  # started_at ties -> session_id ascending
    for _ in range(5):
        rows = await store.list_sessions(node_id)
        assert [r.session_id for r in rows] == expected


async def test_the_no_limit_path_orders_by_started_at_first_when_distinct(
    store: Registry, migrated: DatabaseSettings
) -> None:
    """Confirms N3 did not accidentally make ``session_id`` the primary key:
    creation order (session_id) is deliberately made to disagree with
    ``started_at`` order, and ``started_at`` must still win."""
    node_id, session_ids = await _node_with_sessions(store, 3)
    reversed_order = list(reversed(session_ids))
    await _set_started_at(
        migrated,
        {
            sid: SHARED_TIMESTAMP + dt.timedelta(minutes=i)
            for i, sid in enumerate(reversed_order)
        },
    )
    rows = await store.list_sessions(node_id)
    assert [r.session_id for r in rows] == reversed_order


async def test_a_node_id_filter_still_excludes_other_nodes_sessions(
    store: Registry, migrated: DatabaseSettings
) -> None:
    node_a, sessions_a = await _node_with_sessions(store, 2)
    _node_b, sessions_b = await _node_with_sessions(store, 2)
    rows_a = await store.list_sessions(node_a, limit=10)
    assert {r.session_id for r in rows_a} == set(sessions_a)
    assert not ({r.session_id for r in rows_a} & set(sessions_b))
