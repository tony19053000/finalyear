"""V0.9 G1 tester round 1: migration 0021 up/down with data in the tables."""

# ruff: noqa: E501, B017
from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component


def _sql(
    settings: DatabaseSettings, statements: list[tuple[str, dict[str, Any]]]
) -> list[Any]:
    async def go() -> list[Any]:
        engine = create_engine(settings)
        out: list[Any] = []
        try:
            async with engine.begin() as conn:
                for stmt, params in statements:
                    result = await conn.execute(text(stmt), params)
                    out.append(result.fetchall() if result.returns_rows else None)
        finally:
            await engine.dispose()
        return out

    return asyncio.run(go())


def _cols(settings: DatabaseSettings, table: str) -> set[str]:
    rows = _sql(
        settings,
        [
            (
                "SELECT column_name FROM information_schema.columns WHERE table_name=:t",
                {"t": table},
            )
        ],
    )[0]
    return {r[0] for r in rows}


def test_0021_downgrade_with_data_keeps_older_rows_and_upgrade_again_works(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    _sql(
        pg_settings,
        [
            (
                "INSERT INTO audit_event (action, actor, details) VALUES "
                "('login', 'u', '{}'::jsonb), ('clock_uncertain', 'controller', '{}'::jsonb)",
                {},
            ),
            (
                "INSERT INTO run_record (schema_version, attempt_id, job_id, attempt_no, "
                "node_id, strategy, placement_mode, attempt_state, failure_class, "
                "attempt_created_at) VALUES (1, 'att_x', 'job_x', 1, 'n', 'mode1', "
                "'auto', 'done', 'ok', now())",
                {},
            ),
        ],
    )
    # the CHECK constraints are live
    with pytest.raises(Exception):
        _sql(
            pg_settings,
            [
                (
                    "INSERT INTO run_record (schema_version, attempt_id, job_id, attempt_no, "
                    "node_id, strategy, placement_mode, attempt_state, failure_class, "
                    "attempt_created_at) VALUES (1, 'att_y', 'job_x', 1, 'n', 'mode1', "
                    "'auto', 'done', 'bogus', now())",
                    {},
                )
            ],
        )
    assert "workload" in _cols(pg_settings, "attempt")

    command.downgrade(cfg, "0020")
    assert "workload" not in _cols(pg_settings, "attempt")
    assert not _cols(pg_settings, "run_record")
    assert not {c for c in _cols(pg_settings, "node_session") if c.startswith("clock_")}
    actions = {
        r[0] for r in _sql(pg_settings, [("SELECT action FROM audit_event", {})])[0]
    }
    assert actions == {"login"}, "older audit rows survive; the new action's rows go"
    with pytest.raises(Exception):  # the 0020 CHECK is back
        _sql(
            pg_settings,
            [
                (
                    "INSERT INTO audit_event (action, actor, details) VALUES ('clock_uncertain','c','{}'::jsonb)",
                    {},
                )
            ],
        )

    command.upgrade(cfg, "head")
    command.check(cfg)
    assert _cols(pg_settings, "run_record")
    command.downgrade(cfg, "base")
