"""V0.14 G2: migration 0031 (``planner_calibration``) on a real PostgreSQL.

Up from 0030 creates the table with its checks and the partial unique index
(at most one ``active`` row); the checks refuse what they must (status,
non-object JSON, negative errors, a rejected row without reason, an over-long
reason); down drops it, up again works twice, ``alembic check`` is clean.
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
    "ck_planner_calibration_status",
    "ck_planner_calibration_factors",
    "ck_planner_calibration_fit_inputs",
    "ck_planner_calibration_error_new",
    "ck_planner_calibration_error_active",
    "ck_planner_calibration_reason",
    "ck_planner_calibration_rejected_has_reason",
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
    rows = _run(
        _q(
            settings,
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'planner_calibration' AND table_schema = 'public'",
        )
    )
    return bool(rows)


def _checks(settings: DatabaseSettings) -> set[str]:
    rows = _run(
        _q(
            settings,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'planner_calibration'::regclass AND contype = 'c'",
        )
    )
    return {str(r[0]) for r in rows}


def _index(settings: DatabaseSettings) -> str | None:
    rows = _run(
        _q(
            settings,
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ux_planner_calibration_active'",
        )
    )
    return None if not rows else str(rows[0][0])


def _insert(settings: DatabaseSettings, **values: Any) -> None:
    row = {
        "status": "superseded",
        "factors": "{}",
        "fit_inputs": "{}",
        "e_new": None,
        "e_active": None,
        "reason": None,
        **values,
    }
    _run(
        _q(
            settings,
            "INSERT INTO planner_calibration (status, factors, fit_inputs, "
            "holdout_error_new, holdout_error_active, reason) VALUES (:status, "
            "CAST(:factors AS jsonb), CAST(:fit_inputs AS jsonb), :e_new, :e_active, "
            ":reason)",
            **row,
        )
    )


def _refused(settings: DatabaseSettings, **values: Any) -> None:
    with pytest.raises(DBAPIError) as caught:
        _insert(settings, **values)
    message = str(caught.value)
    assert "violates" in message, message[:300]


def test_0031_up_checks_index_down_up_and_alembic_check(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0030")
    assert not _table(pg_settings)
    command.upgrade(cfg, "0031")
    assert _table(pg_settings)
    assert _checks(pg_settings) >= CHECKS
    index = _index(pg_settings)
    assert index is not None and "UNIQUE" in index
    assert "WHERE" in index and "'active'" in index

    _insert(pg_settings, status="active", e_new=0.1, e_active=0.2)
    _insert(pg_settings, status="superseded")
    _insert(pg_settings, status="rejected", reason="holdout_error 2 > 1.05 x active 1")
    _insert(pg_settings, status="pinned")
    _insert(pg_settings, reason="x" * 512)
    _refused(pg_settings, status="active")  # a second active row
    _refused(pg_settings, status="ACTIVE")
    _refused(pg_settings, status="retired")
    _refused(pg_settings, status="rejected")  # no reason
    _refused(pg_settings, factors="[]")
    _refused(pg_settings, factors="null")
    _refused(pg_settings, fit_inputs='"x"')
    _refused(pg_settings, e_new=-0.1)
    _refused(pg_settings, e_active=-1.0)
    _refused(pg_settings, reason="x" * 513)
    versions = _run(
        _q(pg_settings, "SELECT version FROM planner_calibration ORDER BY version")
    )
    assert len(versions) == 5

    for _ in range(2):
        command.downgrade(cfg, "0030")
        assert not _table(pg_settings)
        assert _index(pg_settings) is None
        command.upgrade(cfg, "head")
        assert _checks(pg_settings) >= CHECKS
    command.check(cfg)
