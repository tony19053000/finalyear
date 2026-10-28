"""V1.0 G4 (D412): migration 0036 (``ix_attempt_live_created_at``) on a real
PostgreSQL.

The partial index exists after the upgrade with the model's predicate, the
live-attempt polls use it (an index scan, not a sequential scan of every
attempt ever run - checked with EXPLAIN on a table with many terminal rows),
and down/up/``alembic check`` agree with the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import sqlalchemy as sa
from alembic import command

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.job import LIVE_ATTEMPT_SQL
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

_LIVE_POLL = (
    "SELECT attempt.node_id FROM attempt WHERE attempt.state IN "
    "('assigned', 'staging', 'env_ready', 'running', 'uploading') "
    "ORDER BY attempt.created_at"
)


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


def _seed_terminal(pg: DatabaseSettings) -> None:
    """3 000 jobs with one ``done`` attempt each (raw SQL: only the columns
    the constraints need)."""
    asyncio.run(
        _sql(
            pg,
            "INSERT INTO node (node_id, hostname, os, platform, hardware, state,"
            " first_seen_at, last_seen_at, session_epoch) VALUES ('node_x', 'h',"
            " 'o', 'p', '{}', 'offline', now(), now(), 1)",
            "INSERT INTO job (id, name, state, manifest, bundle_digest, placement, "
            "request_hash, attempt_epoch) SELECT 'job_' || lpad(g::text, 26, '0'), "
            "'j', 'succeeded', '{}'::jsonb, 'sha256:' || repeat('ab', 32), "
            "'{}'::jsonb, repeat('h', 64), 1 FROM generate_series(1, 3000) g",
            "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, role) "
            "SELECT 'att_' || lpad(g::text, 26, '0'), 'job_' || lpad(g::text, 26, "
            "'0'), 'node_x', 1, 'done', 'run' FROM generate_series(1, 3000) g",
            "ANALYZE attempt",
        )
    )


def test_0036_adds_the_live_attempt_index(pg_settings: DatabaseSettings) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0036")
    (row,) = asyncio.run(
        _sql(
            pg_settings,
            "SELECT indexdef FROM pg_indexes WHERE indexname = "
            "'ix_attempt_live_created_at'",
        )
    )
    for state in ("assigned", "staging", "env_ready", "running", "uploading"):
        assert f"'{state}'" in row.indexdef
    for state in ("done", "failed", "lost", "fenced"):
        assert f"'{state}'" not in row.indexdef
    assert "'uploading'" in LIVE_ATTEMPT_SQL and "'done'" not in LIVE_ATTEMPT_SQL
    command.check(cfg)  # the model declares the same index
    command.downgrade(cfg, "0035")
    gone = asyncio.run(
        _sql(
            pg_settings,
            "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_attempt_live_created_at'",
        )
    )
    assert gone == []
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_the_live_poll_uses_the_index_not_a_seq_scan(
    pg_settings: DatabaseSettings,
) -> None:
    command.upgrade(alembic_config(pg_settings), "head")
    _seed_terminal(pg_settings)
    plan = asyncio.run(_sql(pg_settings, "EXPLAIN " + _LIVE_POLL))
    text = "\n".join(str(r[0]) for r in plan)
    assert "ix_attempt_live_created_at" in text, text
    assert "Seq Scan on attempt" not in text, text
