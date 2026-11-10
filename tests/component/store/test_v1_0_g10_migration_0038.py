"""V1.0 G10 (D451): migration 0038 adds the operational
``chunk_record.stalled_s`` (nullable double) on a real PostgreSQL; the model
agrees (``alembic check``); down/up keeps the existing rows."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import sqlalchemy as sa
from alembic import command

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]


async def _sql(pg: DatabaseSettings, *statements: str) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result: list[Any] = []
            for statement in statements:
                out = await conn.execute(sa.text(statement))
                result = list(out.all()) if out.returns_rows else []
            return result
    finally:
        await engine.dispose()


def _column(pg: DatabaseSettings) -> list[Any]:
    return asyncio.run(
        _sql(
            pg,
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'chunk_record' AND column_name = 'stalled_s'",
        )
    )


_ROW = (
    "INSERT INTO chunk_record (schema_version, job_id, chunk_index, lease_seq, "
    "lane_attempt_id, node_id, first_item, items, speculative, won, outcome, "
    "failure_class, leased_at) VALUES (3, 'job_' || repeat('0', 26), 0, 1, "
    "'att_' || repeat('0', 26), 'node_x', 0, 2, false, true, 'committed', "
    "'ok', now())"
)


def test_0038_adds_a_nullable_stalled_s_and_round_trips(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0037")
    assert _column(pg_settings) == []
    asyncio.run(_sql(pg_settings, _ROW))
    command.upgrade(cfg, "0038")
    assert [tuple(r) for r in _column(pg_settings)] == [("double precision", "YES")]
    command.check(cfg)  # the model declares the same column
    rows = asyncio.run(_sql(pg_settings, "SELECT stalled_s FROM chunk_record"))
    assert [r.stalled_s for r in rows] == [None]  # old rows: never reported
    asyncio.run(_sql(pg_settings, "UPDATE chunk_record SET stalled_s = 120.5"))
    command.downgrade(cfg, "0037")
    assert _column(pg_settings) == []
    count = asyncio.run(_sql(pg_settings, "SELECT count(*) AS n FROM chunk_record"))
    assert count[0].n == 1  # the record itself stays
    command.upgrade(cfg, "head")
    assert [tuple(r) for r in _column(pg_settings)] == [("double precision", "YES")]
