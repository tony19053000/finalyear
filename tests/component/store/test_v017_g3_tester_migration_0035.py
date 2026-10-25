"""V0.17 G3 tester: migration 0035 with many rows, NOT NULL, unicode, re-run."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import sqlalchemy as sa
from alembic import command

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.timeout(300)]

INSERT = (
    "INSERT INTO node (node_id, hostname, os, platform, hardware, state,"
    " first_seen_at, last_seen_at, session_epoch)"
    " SELECT 'node_' || lpad(g::text, 26, '0'), 'h' || g, 'o', 'p', '{}', 'offline',"
    " now(), now(), 1 FROM generate_series(1, 2000) g"
)


async def _sql(pg: DatabaseSettings, statement: str, **params: object) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(sa.text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


def test_0035_over_many_rows_is_not_null_default_empty_and_unicode_safe(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0034")
    asyncio.run(_sql(pg_settings, INSERT))
    command.upgrade(cfg, "0035")
    rows = asyncio.run(
        _sql(
            pg_settings,
            "SELECT count(*), count(*) FILTER (WHERE display_name = '') FROM node",
        )
    )
    assert rows == [(2000, 2000)]
    # NOT NULL
    with pytest.raises(sa.exc.DBAPIError):
        asyncio.run(_sql(pg_settings, "UPDATE node SET display_name = NULL"))
    # 64 astral characters (256 bytes) fit; 65 do not; text round-trips exactly
    name = "\U0001f4bb" * 64
    asyncio.run(
        _sql(
            pg_settings,
            "UPDATE node SET display_name = :v WHERE hostname = 'h1'",
            v=name,
        )
    )
    got = asyncio.run(
        _sql(pg_settings, "SELECT display_name FROM node WHERE hostname = 'h1'")
    )
    assert got == [(name,)]
    with pytest.raises(sa.exc.DBAPIError):
        asyncio.run(
            _sql(pg_settings, "UPDATE node SET display_name = :v", v=name + "x")
        )
    # re-running upgrade is a no-op; check == model
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    command.check(cfg)
    # down then up again over populated data
    command.downgrade(cfg, "0034")
    assert asyncio.run(_sql(pg_settings, "SELECT count(*) FROM node")) == [(2000,)]
    command.upgrade(cfg, "head")
    assert asyncio.run(
        _sql(pg_settings, "SELECT count(*) FROM node WHERE display_name = ''")
    ) == [(2000,)]
