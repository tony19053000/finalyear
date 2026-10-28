"""V1.0 G4 F-4 (D412): migration 0037 (``attempt.recorded`` +
``ix_attempt_unrecorded``) on a real PostgreSQL.

The upgrade backfills ``recorded = true`` for exactly the attempts that already
have a ``run_record`` (terminal attempts without one - a crash before their
record - and live attempts stay ``false``, so the recorder still writes them),
the partial index holds only the unrecorded terminal rows, down/up keeps the
data and ``alembic check`` agrees with the model.
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
from meshforge.controller.store.models.job import UNRECORDED_ATTEMPT_SQL
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


def _seed(pg: DatabaseSettings) -> None:
    """At 0036: 300 jobs; attempts 1-200 ``done`` with a record, 201-250
    ``failed`` without one, 251-300 ``running``. A minimal ``run_record`` row
    is enough for the backfill's join."""
    asyncio.run(
        _sql(
            pg,
            "INSERT INTO node (node_id, hostname, os, platform, hardware, state,"
            " first_seen_at, last_seen_at, session_epoch) VALUES ('node_x', 'h',"
            " 'o', 'p', '{}', 'offline', now(), now(), 1)",
            "INSERT INTO job (id, name, state, manifest, bundle_digest, placement, "
            "request_hash, attempt_epoch) SELECT 'job_' || lpad(g::text, 26, '0'), "
            "'j', 'succeeded', '{}'::jsonb, 'sha256:' || repeat('ab', 32), "
            "'{}'::jsonb, repeat('h', 64), 1 FROM generate_series(1, 300) g",
            "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, role) "
            "SELECT 'att_' || lpad(g::text, 26, '0'), 'job_' || lpad(g::text, 26, "
            "'0'), 'node_x', 1, CASE WHEN g <= 200 THEN 'done' WHEN g <= 250 THEN"
            " 'failed' ELSE 'running' END, 'run' FROM generate_series(1, 300) g",
        )
    )
    asyncio.run(
        _sql(
            pg,
            "INSERT INTO run_record (schema_version, attempt_id, job_id, attempt_no,"
            " node_id, strategy, placement_mode, attempt_state, failure_class,"
            " attempt_created_at) SELECT 3, a.id, a.job_id, 1, a.node_id, 'mode1',"
            " 'auto', 'done', 'ok', now() FROM attempt a WHERE a.state = 'done'",
        )
    )


def _flags(pg: DatabaseSettings) -> dict[str, list[str]]:
    rows = asyncio.run(
        _sql(
            pg,
            "SELECT state, recorded, count(*) FROM attempt GROUP BY 1, 2 ORDER BY 1, 2",
        )
    )
    return {f"{r[0]}:{r[1]}": r[2] for r in rows}


def _index_def(pg: DatabaseSettings) -> str | None:
    rows = asyncio.run(
        _sql(
            pg,
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_attempt_unrecorded'",
        )
    )
    return rows[0].indexdef if rows else None


def test_0037_backfills_exactly_the_recorded_attempts(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0036")
    _seed(pg_settings)
    command.upgrade(cfg, "0037")
    assert _flags(pg_settings) == {
        "done:True": 200,
        "failed:False": 50,
        "running:False": 50,
    }
    indexdef = _index_def(pg_settings)
    assert indexdef is not None
    assert "(created_at, id)" in indexdef
    assert "NOT recorded" in indexdef
    for state in ("done", "failed", "fenced", "lost"):
        assert f"'{state}'" in indexdef
    assert "'running'" not in indexdef
    assert "NOT recorded" in UNRECORDED_ATTEMPT_SQL
    command.check(cfg)  # the model declares the same column and index
    # the index holds only the 50 unrecorded terminal rows
    (row,) = asyncio.run(
        _sql(
            pg_settings,
            f"SELECT count(*) AS n FROM attempt WHERE {UNRECORDED_ATTEMPT_SQL}",  # noqa: S608
        )
    )
    assert row.n == 50


def test_0037_down_and_up_keep_the_data_and_rebackfill(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0036")
    _seed(pg_settings)
    command.upgrade(cfg, "head")
    before = asyncio.run(_sql(pg_settings, "SELECT id, state FROM attempt ORDER BY id"))
    command.downgrade(cfg, "0036")
    assert _index_def(pg_settings) is None
    cols = asyncio.run(
        _sql(
            pg_settings,
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'attempt'"
            " AND column_name = 'recorded'",
        )
    )
    assert cols == []
    (records,) = asyncio.run(_sql(pg_settings, "SELECT count(*) AS n FROM run_record"))
    assert records.n == 200  # the dataset is never dropped
    command.upgrade(cfg, "head")
    after = asyncio.run(_sql(pg_settings, "SELECT id, state FROM attempt ORDER BY id"))
    assert before == after
    assert _flags(pg_settings)["done:True"] == 200
    command.check(cfg)


def test_0037_on_an_empty_database(pg_settings: DatabaseSettings) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    assert _index_def(pg_settings) is not None
    command.check(cfg)
