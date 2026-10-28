"""V0.17 G3: migration 0035 (``node.display_name``) on a real PostgreSQL.

Existing rows get ``''`` with no backfill, the column holds 64 characters, the
registry writes it on every Hello (empty clears it) and reads it back, and
down/up/``alembic check`` agree with the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import sqlalchemy as sa
from alembic import command

from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

NODE = "node_01ARZ3NDEKTSV4RRFFQ69G5FAV"


async def _sql(pg: DatabaseSettings, statement: str, **params: object) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(sa.text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


def test_0035_adds_display_name_without_backfill(pg_settings: DatabaseSettings) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0034")
    asyncio.run(
        _sql(
            pg_settings,
            "INSERT INTO node (node_id, hostname, os, platform, hardware, state,"
            " first_seen_at, last_seen_at, session_epoch) VALUES (:n, 'h', 'o',"
            " 'p', '{}', 'offline', now(), now(), 1)",
            n=NODE,
        )
    )
    command.upgrade(cfg, "0035")
    rows = asyncio.run(_sql(pg_settings, "SELECT display_name FROM node"))
    assert rows == [("",)]
    asyncio.run(_sql(pg_settings, "UPDATE node SET display_name = :v", v="x" * 64))
    with pytest.raises(sa.exc.DBAPIError):
        asyncio.run(_sql(pg_settings, "UPDATE node SET display_name = :v", v="x" * 65))
    # ``check`` needs the head revision: done after the round trip below
    command.downgrade(cfg, "0034")
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_registry_writes_and_reads_the_display_name(
    pg_settings: DatabaseSettings,
) -> None:
    command.upgrade(alembic_config(pg_settings), "head")
    asyncio.run(_registry_round_trip(pg_settings))


async def _registry_round_trip(pg_settings: DatabaseSettings) -> None:
    engine = create_engine(pg_settings)
    try:
        registry = Registry(engine)
        common = {
            "node_id": NODE,
            "hostname": "lab-a",
            "os": "Ubuntu",
            "platform": "linux-x86_64",
            "hardware": {},
            "state": NodeState.ONLINE,
        }
        rec = await registry.upsert_node_on_hello(
            **common, session_epoch=1, session_id="sess_1", display_name="Lab A"
        )
        assert rec.display_name == "Lab A"
        rec = await registry.upsert_node_on_hello(
            **common, session_epoch=2, session_id="sess_2", display_name="Lab B"
        )
        assert rec.display_name == "Lab B"
        rec = await registry.upsert_node_on_hello(
            **common, session_epoch=3, session_id="sess_3"
        )
        assert rec.display_name == ""  # a Hello without a name clears it
    finally:
        await engine.dispose()
