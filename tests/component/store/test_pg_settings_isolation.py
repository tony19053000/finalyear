"""Tester (task 0.1.8): `pg_settings` (tests/component/conftest.py) actually gives
each test its own, disposable database -- not just a fresh schema in a shared one.

Two tests create a table with the *same name* but incompatible column types and
different data; if `pg_settings` leaked state between tests (same physical
database, or DROP DATABASE failing silently), one of the two would fail with a
duplicate-table or type-mismatch error, or would see the other test's row. A
third test proves the database from the *first* test is really gone (not just
empty) by trying to connect to it directly and observing the connection fail.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("asyncpg", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import asyncpg

from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component

_DROPPED_DB_NAME: str | None = None


async def _run(settings: DatabaseSettings, *statements: str) -> None:
    conn = await asyncpg.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password.get_secret_value(),
        database=settings.name,
        timeout=10,
    )
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


async def _fetchval(settings: DatabaseSettings, query: str) -> object:
    conn = await asyncpg.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password.get_secret_value(),
        database=settings.name,
        timeout=10,
    )
    try:
        return await conn.fetchval(query)
    finally:
        await conn.close()


def test_same_table_name_first_database(pg_settings: DatabaseSettings) -> None:
    global _DROPPED_DB_NAME
    _DROPPED_DB_NAME = pg_settings.name
    asyncio.run(
        _run(
            pg_settings,
            "CREATE TABLE widgets (id INTEGER PRIMARY KEY, note TEXT)",
            "INSERT INTO widgets (id, note) VALUES (1, 'from test A')",
        )
    )
    note = asyncio.run(_fetchval(pg_settings, "SELECT note FROM widgets WHERE id = 1"))
    assert note == "from test A"


def test_same_table_name_second_database_does_not_see_first(
    pg_settings: DatabaseSettings,
) -> None:
    """A different physical database: an incompatible schema for the same table
    name must work with no duplicate-object error, and the row from the other
    test's database must not be visible here.
    """
    if _DROPPED_DB_NAME is None:
        pytest.skip(
            "must run after test_same_table_name_first_database in the same "
            "session (default in-file order; skip if run standalone/reordered)"
        )
    assert pg_settings.name != _DROPPED_DB_NAME, (
        "pg_settings must hand out a distinct database per test"
    )
    asyncio.run(
        _run(
            pg_settings,
            # Same table name, incompatible column set/types vs test A.
            "CREATE TABLE widgets (widget_uuid TEXT PRIMARY KEY, weight_kg NUMERIC)",
            "INSERT INTO widgets (widget_uuid, weight_kg) VALUES ('w-1', 3.5)",
        )
    )
    count = asyncio.run(_fetchval(pg_settings, "SELECT count(*) FROM widgets"))
    assert count == 1, "must not see test A's row(s) in a fresh database"


def test_first_database_was_actually_dropped_after_its_test(
    pg_settings: DatabaseSettings,
) -> None:
    """`pg_settings` drops the database in its teardown; verify by connecting
    directly to the name used by the first test above (not through the fixture,
    which would create a brand-new one under a fresh UUID).
    """
    if _DROPPED_DB_NAME is None:
        pytest.skip(
            "must run after test_same_table_name_first_database in the same "
            "session (default in-file order; skip if run standalone/reordered)"
        )
    dropped_name = _DROPPED_DB_NAME

    async def try_connect() -> None:
        conn = await asyncpg.connect(
            host=pg_settings.host,
            port=pg_settings.port,
            user=pg_settings.user,
            password=pg_settings.password.get_secret_value(),
            database=dropped_name,
            timeout=10,
        )
        await conn.close()

    with pytest.raises(asyncpg.InvalidCatalogNameError):
        asyncio.run(try_connect())
