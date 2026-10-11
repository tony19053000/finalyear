"""V0.7 G3 tester round 2: migration 0017 (end reasons cert_superseded /
cert_expired) against real PostgreSQL: the CHECK really admits the new values at
0017, really rejects them at 0016, a downgrade rewrites rows that carry them to
``revoked`` (never a violated constraint, never a lost row), and the upgrade is
repeatable."""

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
from meshforge.controller.store.models.node import SessionEndReason
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component

NEW = ("cert_superseded", "cert_expired")


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


_FILL = {
    "character varying": "'x'",
    "text": "'x'",
    "integer": "0",
    "bigint": "0",
    "smallint": "0",
    "numeric": "0",
    "double precision": "0",
    "real": "0",
    "boolean": "false",
    "jsonb": "'{}'::jsonb",
    "json": "'{}'::json",
    "timestamp with time zone": "now()",
    "timestamp without time zone": "now()",
}


async def _insert_session(
    settings: DatabaseSettings, node_id: str, reason: str | None
) -> None:
    """A node row and a node_session row, every NOT NULL column filled."""
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            for table, extra in (
                ("node", {"node_id": node_id, "state": "offline"}),
                (
                    "node_session",
                    {
                        "node_id": node_id,
                        "session_id": f"ses_{node_id}",
                        "end_reason": reason,
                    },
                ),
            ):
                cols = (
                    await conn.execute(
                        text(
                            "SELECT column_name, data_type, column_default "
                            "FROM information_schema.columns "
                            "WHERE table_name = :t AND is_nullable = 'NO'"
                        ),
                        {"t": table},
                    )
                ).all()
                values: dict[str, str] = {}
                for name, data_type, default in cols:
                    if name in extra:
                        continue
                    if default is not None:
                        continue
                    values[name] = _FILL[data_type]
                names = list(values) + list(extra)
                sql_values = list(values.values()) + [f":{k}" for k in extra]
                await conn.execute(
                    text(
                        f"INSERT INTO {table} ({', '.join(names)}) "  # noqa: S608
                        f"VALUES ({', '.join(sql_values)})"
                    ),
                    extra,
                )
    finally:
        await engine.dispose()


async def _scalar(settings: DatabaseSettings, sql: str, **p: Any) -> Any:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            return (await conn.execute(text(sql), p)).all()
    finally:
        await engine.dispose()


def test_0017_admits_the_new_end_reasons_and_the_downgrade_rewrites_them(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0016")
    # at 0016 the new values are refused by the database itself
    with pytest.raises(IntegrityError, match="end_reason"):
        _run(_insert_session(pg_settings, "node_REJECTED", "cert_expired"))
    command.upgrade(cfg, "0017")
    for index, reason in enumerate((*NEW, "revoked", None)):
        _run(_insert_session(pg_settings, f"node_N{index}", reason))
    rows = _run(_scalar(pg_settings, "SELECT node_id, end_reason FROM node_session"))
    assert {r[1] for r in rows} == {*NEW, "revoked", None}
    with pytest.raises(
        IntegrityError, match="end_reason"
    ):  # still a closed list at 0017
        _run(_insert_session(pg_settings, "node_BOGUS", "cert_bogus"))

    command.downgrade(cfg, "0016")
    rows = dict(
        _run(_scalar(pg_settings, "SELECT node_id, end_reason FROM node_session"))
    )
    assert rows == {
        "node_N0": "revoked",
        "node_N1": "revoked",
        "node_N2": "revoked",
        "node_N3": None,
    }, "downgrade lost or kept a 0017-only reason"
    command.upgrade(cfg, "0017")  # and back up again, data intact
    assert len(_run(_scalar(pg_settings, "SELECT 1 FROM node_session"))) == 4
    command.upgrade(cfg, "head")  # V0.7 G4: the models are at 0018 now
    command.check(cfg)
    command.downgrade(cfg, "base")


def test_every_model_end_reason_is_admitted_by_the_head_check(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    try:
        for index, reason in enumerate(SessionEndReason):
            _run(_insert_session(pg_settings, f"node_E{index}", reason.value))
    finally:
        command.downgrade(cfg, "base")
