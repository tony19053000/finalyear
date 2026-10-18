"""V0.9 G4 lane A item 0.3 (D233.3/D241): migration 0023 - telemetry schema v2
columns on ``run_record`` (``result_schema_version``, ``condition``,
``condition_measured``) up/down, with a v1 row written before the upgrade kept
as it was."""

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
from meshforge.controller.store.models.telemetry import _CONDITION
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry.record import CONDITION_CHARS

pytestmark = pytest.mark.component

V2_COLUMNS = {"result_schema_version", "condition", "condition_measured"}


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


def _cols(settings: DatabaseSettings) -> set[str]:
    rows = _sql(
        settings,
        [
            (
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'run_record'",
                {},
            )
        ],
    )[0]
    return {r[0] for r in rows}


_V1_INSERT = (
    "INSERT INTO run_record (schema_version, attempt_id, job_id, attempt_no, node_id, "
    "strategy, placement_mode, attempt_state, failure_class, attempt_created_at) VALUES "
    "(1, :a, 'job_1', 1, 'node_a', 'local', 'auto', 'done', 'ok', now())"
)
_V2_INSERT = (
    "INSERT INTO run_record (schema_version, attempt_id, job_id, attempt_no, node_id, "
    "strategy, placement_mode, attempt_state, failure_class, attempt_created_at, "
    "result_schema_version, condition, condition_measured) VALUES "
    "(2, :a, 'job_2', 1, 'node_a', 'mode1', 'pinned', 'done', 'ok', now(), 1, :c, "
    "CAST(:m AS jsonb))"
)


def _v1_row(attempt: str) -> tuple[str, dict[str, Any]]:
    return (_V1_INSERT, {"a": attempt})


def _v2_row(attempt: str, condition: str) -> tuple[str, dict[str, Any]]:
    return (_V2_INSERT, {"a": attempt, "c": condition, "m": '{"cpu_busy_cores": 1.9}'})


def test_the_condition_width_is_one_number() -> None:
    assert _CONDITION == CONDITION_CHARS == 128


def test_0023_adds_the_v2_columns_keeps_v1_rows_and_downgrades(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0022")
    assert not V2_COLUMNS & _cols(pg_settings)
    _sql(pg_settings, [_v1_row("att_v1")])  # a record written under v1

    command.upgrade(cfg, "head")
    command.check(cfg)
    assert _cols(pg_settings) >= V2_COLUMNS
    _sql(pg_settings, [_v2_row("att_v2", "cpu_load:2")])
    rows = _sql(
        pg_settings,
        [
            (
                "SELECT attempt_id, schema_version, result_schema_version, condition, "
                "condition_measured FROM run_record ORDER BY attempt_id",
                {},
            )
        ],
    )[0]
    assert [tuple(r) for r in rows] == [
        ("att_v1", 1, None, None, None),  # untouched: never rewritten
        ("att_v2", 2, 1, "cpu_load:2", {"cpu_busy_cores": 1.9}),
    ]
    with pytest.raises(DBAPIError):  # wider than the column: refused, not cut
        _sql(pg_settings, [_v2_row("att_long", "x" * (CONDITION_CHARS + 1))])

    command.downgrade(cfg, "0022")
    assert not V2_COLUMNS & _cols(pg_settings)
    assert _sql(pg_settings, [("SELECT count(*) FROM run_record", {})])[0][0][0] == 2

    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, "base")
