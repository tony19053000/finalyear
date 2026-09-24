"""V0.3 G6 (D19): migration 0004 against a real PostgreSQL.

``stream_lost`` must be storable at head, refused at 0003, and a downgrade must
rewrite existing ``stream_lost`` rows (to ``agent_closed``, what they were recorded
as before G6) instead of failing on real data. Synchronous like every Alembic test
here: ``env.py`` runs its own ``asyncio.run``.
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
from sqlalchemy.exc import IntegrityError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component

_NODE = "node_01J00000000000000000000001"
_SESSION = "sess_01J00000000000000000000001"


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


_SET_STREAM_LOST = (
    "UPDATE node_session SET end_reason = 'stream_lost' WHERE session_id = :s"
)


def _seed(settings: DatabaseSettings, end_reason: str) -> None:
    _run(
        _exec(
            settings,
            "INSERT INTO node (node_id, hostname, os, platform, hardware, state, "
            "first_seen_at, last_seen_at, last_session_id, session_epoch) VALUES "
            "(:n, 'h', 'o', 'p', '{}', 'offline', now(), now(), :s, 1)",
            n=_NODE,
            s=_SESSION,
        )
    )
    _run(
        _exec(
            settings,
            "INSERT INTO node_session (session_id, node_id, session_epoch, "
            "started_at, ended_at, end_reason, peer_address, agent_version, "
            "protocol_version) VALUES (:s, :n, 1, now(), now(), :r, "
            "'ipv4:127.0.0.1:1', '0.1.0', 1)",
            n=_NODE,
            s=_SESSION,
            r=end_reason,
        )
    )


def test_stream_lost_is_stored_at_head_and_rewritten_by_the_downgrade(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    _seed(pg_settings, "stream_lost")

    command.downgrade(cfg, "0003")
    rows = _run(
        _exec(
            pg_settings,
            "SELECT end_reason FROM node_session WHERE session_id = :s",
            s=_SESSION,
        )
    )
    assert [r[0] for r in rows] == ["agent_closed"]
    # 0003's CHECK is back: stream_lost is refused again.
    with pytest.raises(IntegrityError):
        _run(_exec(pg_settings, _SET_STREAM_LOST, s=_SESSION))

    command.upgrade(cfg, "head")
    _run(_exec(pg_settings, _SET_STREAM_LOST, s=_SESSION))
    command.check(cfg)  # model == migrations after the round trip
    command.downgrade(cfg, "base")
