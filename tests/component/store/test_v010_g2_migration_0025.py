"""V0.10 G2 (D262): migration 0025 - the work-queue tables up/down on real
PostgreSQL, ``alembic check`` clean, and the CHECKs / partial unique index bite."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component

TABLES = {"work_queue", "work_item", "work_lease"}


def _sql(
    settings: DatabaseSettings, stmt: str, params: dict[str, Any] | None = None
) -> Any:
    async def go() -> Any:
        engine = create_engine(settings)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(stmt), params or {})
                return result.fetchall() if result.returns_rows else None
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _tables(settings: DatabaseSettings) -> set[str]:
    rows = _sql(
        settings,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    )
    return {r[0] for r in rows}


_QUEUE = (
    "INSERT INTO work_queue (kind, owner_id, state, visibility_timeout_s, "
    "max_attempts, count_expiry, speculative_max_frac, item_count) VALUES "
    "('k', :o, :state, 60, 3, false, 0.1, 2) RETURNING id"
)
_ITEM = (
    "INSERT INTO work_item (queue_id, idx, payload, state, result_digest, "
    "done_lease_seq, failed_kind) VALUES (:q, :idx, '{}'::jsonb, :state, :d, :s, :f) "
    "RETURNING id"
)
_LEASE = (
    "INSERT INTO work_lease (item_id, seq, holder, site, state, speculative, "
    "leased_at, deadline) VALUES (:i, :seq, 'h', 'n', :state, :spec, now(), "
    "now() + interval '1 minute')"
)


def test_0025_creates_the_tables_constraints_bite_and_downgrade_drops_them(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0024")
    assert not TABLES & _tables(pg_settings)
    command.upgrade(cfg, "head")
    command.check(cfg)
    assert _tables(pg_settings) >= TABLES

    qid = _sql(pg_settings, _QUEUE, {"o": "a", "state": "open"})[0][0]
    item = {"q": qid, "idx": 0, "state": "leased", "d": None, "s": None, "f": None}
    iid = _sql(pg_settings, _ITEM, item)[0][0]
    _sql(pg_settings, _LEASE, {"i": iid, "seq": 1, "state": "live", "spec": False})
    _sql(pg_settings, _LEASE, {"i": iid, "seq": 2, "state": "live", "spec": True})
    _sql(pg_settings, _LEASE, {"i": iid, "seq": 3, "state": "expired", "spec": False})

    refused: list[tuple[str, dict[str, Any]]] = [
        (_QUEUE, {"o": "b", "state": "paused"}),  # unknown queue state
        (_QUEUE, {"o": "a", "state": "open"}),  # one queue per owner
        (_ITEM, {**item, "idx": 0}),  # (queue, idx) unique
        (_ITEM, {**item, "idx": 1, "state": "done"}),  # done without a result
        (_ITEM, {**item, "idx": 1, "state": "failed"}),  # failed without a kind
        (_ITEM, {**item, "idx": -1}),
        (_LEASE, {"i": iid, "seq": 4, "state": "live", "spec": False}),  # 2nd ordinary
        (_LEASE, {"i": iid, "seq": 4, "state": "live", "spec": True}),  # 2nd copy
        (_LEASE, {"i": iid, "seq": 1, "state": "failed", "spec": False}),  # seq unique
        (_LEASE, {"i": iid, "seq": 0, "state": "failed", "spec": False}),
        (_LEASE, {"i": iid, "seq": 5, "state": "lost", "spec": False}),
    ]
    for stmt, params in refused:
        with pytest.raises(DBAPIError):
            _sql(pg_settings, stmt, params)

    command.downgrade(cfg, "0024")
    assert not TABLES & _tables(pg_settings)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
