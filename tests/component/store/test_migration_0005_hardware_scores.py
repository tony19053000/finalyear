"""V0.3 G7 (D38): migration 0005 against a real PostgreSQL.

``node.hardware_scores`` is a nullable JSONB column at head, absent at 0004, and
the downgrade drops it without failing on rows that carry scores. Synchronous like
every Alembic test here: ``env.py`` runs its own ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component

_NODE = "node_01J00000000000000000000001"
_SCORES = (
    '{"run_id": "score_01J00000000000000000000001", '
    '"measured_at": "2026-09-24T12:00:00.000000+00:00"}'
)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _exec(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result) if result.returns_rows else []
    finally:
        await engine.dispose()


def test_hardware_scores_column_round_trips_through_upgrade_and_downgrade(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    _run(
        _exec(
            pg_settings,
            "INSERT INTO node (node_id, hostname, os, platform, hardware, state, "
            "first_seen_at, last_seen_at, session_epoch) VALUES "
            "(:n, 'h', 'o', 'p', '{}', 'offline', now(), now(), 1)",
            n=_NODE,
        )
    )
    rows = _run(
        _exec(
            pg_settings,
            "SELECT hardware_scores IS NULL FROM node WHERE node_id = :n",
            n=_NODE,
        )
    )
    assert [r[0] for r in rows] == [True]  # NULL by default: pending/none
    _run(
        _exec(
            pg_settings,
            "UPDATE node SET hardware_scores = CAST(:s AS JSONB) WHERE node_id = :n",
            s=_SCORES,
            n=_NODE,
        )
    )
    rows = _run(
        _exec(
            pg_settings,
            "SELECT (hardware_scores->>'measured_at')::timestamptz FROM node",
        )
    )
    assert rows[0][0].isoformat() == "2026-09-24T12:00:00+00:00"

    command.downgrade(cfg, "0004")  # drops the column, data and all
    with pytest.raises(ProgrammingError):
        _run(_exec(pg_settings, "SELECT hardware_scores FROM node"))
    assert _run(_exec(pg_settings, "SELECT count(*) FROM node"))[0][0] == 1

    command.upgrade(cfg, "head")
    command.check(cfg)  # model == migrations after the round trip
    command.downgrade(cfg, "base")
