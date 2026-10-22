"""V0.14 G4: migration 0032 (``planner_eval_report``) on a real PostgreSQL.

Up from 0031 creates the table, its checks and the ``created_at`` index; the
checks refuse what they must (a non-array ``campaign_ids``, a non-object
``report``, an empty or over-long ``holdout``); down drops it, up again works
twice, ``alembic check`` is clean (model == migration).
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
from sqlalchemy.exc import DBAPIError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component

CHECKS = {
    "ck_planner_eval_report_campaign_ids",
    "ck_planner_eval_report_report",
    "ck_planner_eval_report_holdout",
}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _q(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return list(res.all()) if res.returns_rows else []
    finally:
        await engine.dispose()


def _table(settings: DatabaseSettings) -> bool:
    return bool(
        _run(
            _q(
                settings,
                "SELECT 1 FROM information_schema.tables WHERE table_name = "
                "'planner_eval_report' AND table_schema = 'public'",
            )
        )
    )


def _checks(settings: DatabaseSettings) -> set[str]:
    rows = _run(
        _q(
            settings,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'planner_eval_report'::regclass AND contype = 'c'",
        )
    )
    return {str(r[0]) for r in rows}


def _index(settings: DatabaseSettings) -> bool:
    return bool(
        _run(
            _q(
                settings,
                "SELECT 1 FROM pg_indexes "
                "WHERE indexname = 'ix_planner_eval_report_created_at'",
            )
        )
    )


def _insert(settings: DatabaseSettings, **values: Any) -> None:
    row = {"ids": '["camp_1"]', "holdout": None, "leaked": False, "report": "{}"}
    row.update(values)
    _run(
        _q(
            settings,
            "INSERT INTO planner_eval_report (campaign_ids, holdout, leaked, report) "
            "VALUES (CAST(:ids AS jsonb), :holdout, :leaked, CAST(:report AS jsonb))",
            **row,
        )
    )


def _refused(settings: DatabaseSettings, **values: Any) -> None:
    with pytest.raises(DBAPIError) as caught:
        _insert(settings, **values)
    assert "violates" in str(caught.value), str(caught.value)[:300]


def test_0032_up_checks_down_up_and_alembic_check(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0031")
    assert not _table(pg_settings)
    command.upgrade(cfg, "0032")
    assert _table(pg_settings) and _index(pg_settings)
    assert _checks(pg_settings) >= CHECKS

    _insert(pg_settings)
    _insert(pg_settings, holdout="cpu.matmul", leaked=True)
    _insert(pg_settings, holdout="x" * 64)
    _refused(pg_settings, ids='{"a": 1}')
    _refused(pg_settings, ids='"camp_1"')
    _refused(pg_settings, report="[]")
    _refused(pg_settings, report="null")
    _refused(pg_settings, holdout="")
    _refused(pg_settings, holdout="x" * 65)
    rows = _run(_q(pg_settings, "SELECT id, created_at FROM planner_eval_report"))
    assert len(rows) == 3 and all(r[1] is not None for r in rows)

    for _ in range(2):
        command.downgrade(cfg, "0031")
        assert not _table(pg_settings) and not _index(pg_settings)
        command.upgrade(cfg, "head")
        assert _checks(pg_settings) >= CHECKS
    command.check(cfg)
