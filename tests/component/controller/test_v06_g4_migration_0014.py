"""V0.6 G4: migration 0014 on real PostgreSQL (D108, D111).

* ``network_link``: a pair with itself, a negative RTT, a zero bandwidth and
  zero samples are refused; a row goes with its node (``ON DELETE CASCADE``);
* ``attempt.env_hash`` must be 64 lowercase hex; ``attempt.output_bytes``
  stays in the uint64 domain;
* down/up cycles are clean (``alembic check`` after each).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio


async def _sql(
    pg: DatabaseSettings, statement: str, params: dict[str, Any] | None = None
) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrated(pg_settings: DatabaseSettings) -> AsyncIterator[DatabaseSettings]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    yield pg_settings


async def _node(pg: DatabaseSettings) -> str:
    node_id = str(new_node_id())
    await _sql(
        pg,
        "INSERT INTO node (node_id, hostname, os, platform, hardware, state, "
        "first_seen_at, last_seen_at, session_epoch) VALUES (:n, 'h', 'l', 'p', "
        "'{}', 'online', now(), now(), 1)",
        {"n": node_id},
    )
    return node_id


def _link(src: str, dst: str, **kw: Any) -> dict[str, Any]:
    return {"s": src, "d": dst, "r": 1.0, "b": 1.0, "n": 1, **kw}


_INSERT = (
    "INSERT INTO network_link (src_node_id, dst_node_id, rtt_ms, bw_bytes_s, "
    "samples, measured_at) VALUES (:s, :d, :r, :b, :n, now())"
)


async def test_network_link_checks_and_cascade(migrated: DatabaseSettings) -> None:
    a, b = await _node(migrated), await _node(migrated)
    await _sql(migrated, _INSERT, _link(a, b))
    for bad in (_link(a, a), _link(b, a, r=-0.1), _link(b, a, b=0.0), _link(b, a, n=0)):
        with pytest.raises(IntegrityError):
            await _sql(migrated, _INSERT, bad)
    await _sql(migrated, "DELETE FROM node WHERE node_id = :n", {"n": b})
    assert await _sql(migrated, "SELECT count(*) FROM network_link") == [(0,)]


async def test_attempt_env_hash_and_output_bytes_checks(
    migrated: DatabaseSettings,
) -> None:
    node = await _node(migrated)
    job, attempt = str(new_job_id()), str(new_attempt_id())
    await _sql(
        migrated,
        "INSERT INTO job (id, name, state, manifest, bundle_digest, placement, "
        "request_hash, created_at, updated_at) VALUES (:id, 'j', 'running', '{}', "
        ":digest, '{}', :hash, now(), now())",
        {"id": job, "digest": "sha256:" + "cd" * 32, "hash": "a" * 64},
    )
    await _sql(
        migrated,
        "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, "
        "created_at, updated_at) VALUES (:a, :j, :n, 1, 'running', now(), now())",
        {"a": attempt, "j": job, "n": node},
    )
    update = "UPDATE attempt SET {col} = :v WHERE id = :a"
    await _sql(migrated, update.format(col="env_hash"), {"v": "e" * 64, "a": attempt})
    await _sql(
        migrated, update.format(col="output_bytes"), {"v": 2**64 - 1, "a": attempt}
    )
    for col, bad in (
        ("env_hash", "E" * 64),
        ("env_hash", "e" * 63),
        ("output_bytes", -1),
        ("output_bytes", 2**64),
    ):
        with pytest.raises(IntegrityError):
            await _sql(migrated, update.format(col=col), {"v": bad, "a": attempt})


async def test_down_up_cycles_are_clean(migrated: DatabaseSettings) -> None:
    cfg = alembic_config(migrated)
    for _ in range(2):
        await asyncio.to_thread(command.downgrade, cfg, "0013")
        tables = await _sql(
            migrated,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'network_link'",
        )
        assert tables == [(0,)]
        columns = await _sql(
            migrated,
            "SELECT count(*) FROM information_schema.columns WHERE "
            "table_name = 'attempt' AND column_name IN ('env_hash', 'output_bytes')",
        )
        assert columns == [(0,)]
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.check, cfg)
