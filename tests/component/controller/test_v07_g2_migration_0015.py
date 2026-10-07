"""V0.7 G2: migration 0015 on real PostgreSQL (D135, D136, D151).

* ``join_token``: the hash must be 64 lower-case hex and unique; expiry after
  creation; ``used_at`` and ``used_by_node`` together; used and revoked never
  both;
* ``node`` certificate columns: all four or none; hex shapes; serial unique;
  rows from before the migration keep NULLs (never enrolled);
* ``audit_event``: known actions only, ``details`` a bounded JSON object;
* down/up cycles are clean (``alembic check`` after each) and the downgrade
  removes every table/column it added.
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
from sqlalchemy.exc import DBAPIError, IntegrityError

from meshforge.common.ids import new_invite_id, new_node_id
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.enrollment import MAX_AUDIT_DETAILS_BYTES
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.component, pytest.mark.docker]


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


_NODE = (
    "INSERT INTO node (node_id, hostname, os, platform, hardware, state, "
    "first_seen_at, last_seen_at, session_epoch) VALUES (:n, 'h', 'l', 'p', "
    "'{}', 'online', now(), now(), 1)"
)
_TOKEN = (
    "INSERT INTO join_token (id, token_hash, created_by, created_at, expires_at, "
    "used_at, used_by_node, revoked_at) VALUES (:id, :h, 'test', now(), "
    "now() + make_interval(secs => :ttl), :used, :by, :revoked)"
)


def _token(**kw: Any) -> dict[str, Any]:
    return {
        "id": str(new_invite_id()),
        "h": "ab" * 32,
        "ttl": 900,
        "used": None,
        "by": None,
        "revoked": None,
        **kw,
    }


async def test_join_token_checks(migrated: DatabaseSettings) -> None:
    await _sql(migrated, _TOKEN, _token())
    node = str(new_node_id())
    await _sql(
        migrated,
        _TOKEN.replace(":used", "now()"),
        _token(h="cd" * 32, by=node),
    )
    bad = [
        _token(h="ab" * 32),  # duplicate hash
        _token(h="AB" * 32),  # upper case
        _token(h="ab" * 31),  # short
        _token(h="ef" * 32, ttl=0),  # expires at creation
        _token(h="ef" * 32, by=node),  # used_by without used_at
    ]
    for params in bad:
        with pytest.raises(IntegrityError):
            await _sql(migrated, _TOKEN, params)
    with pytest.raises(IntegrityError):  # used AND revoked
        await _sql(
            migrated,
            _TOKEN.replace(":used", "now()").replace(":revoked", "now()"),
            _token(h="ef" * 32, by=node),
        )


async def test_node_cert_columns(migrated: DatabaseSettings) -> None:
    a, b = str(new_node_id()), str(new_node_id())
    await _sql(migrated, _NODE, {"n": a})
    await _sql(migrated, _NODE, {"n": b})
    assert await _sql(
        migrated,
        "SELECT cert_serial, cert_fingerprint, cert_not_after, "
        "public_key_fingerprint FROM node WHERE node_id = :n",
        {"n": a},
    ) == [(None, None, None, None)]
    full = (
        "UPDATE node SET cert_serial = :s, cert_fingerprint = :f, "
        "cert_not_after = now(), public_key_fingerprint = :k WHERE node_id = :n"
    )
    await _sql(migrated, full, {"s": "1f" * 20, "f": "a" * 64, "k": "b" * 64, "n": a})
    for params in (
        {"s": "1f" * 20, "f": "a" * 64, "k": "b" * 64, "n": b},  # serial unique
        {"s": "XYZ", "f": "a" * 64, "k": "b" * 64, "n": b},
        {"s": "01", "f": "A" * 64, "k": "b" * 64, "n": b},
        {"s": "01", "f": "a" * 64, "k": "b" * 63, "n": b},
    ):
        with pytest.raises(IntegrityError):
            await _sql(migrated, full, params)
    with pytest.raises(DBAPIError, match="too long"):  # > 160 bits
        await _sql(
            migrated, full, {"s": "1f" * 21, "f": "a" * 64, "k": "b" * 64, "n": b}
        )
    with pytest.raises(IntegrityError):  # half set
        await _sql(
            migrated, "UPDATE node SET cert_serial = '02' WHERE node_id = :n", {"n": b}
        )


async def test_audit_event_checks(migrated: DatabaseSettings) -> None:
    insert = (
        "INSERT INTO audit_event (action, actor, details) "
        "VALUES (:a, 'test', CAST(:d AS jsonb))"
    )
    await _sql(migrated, insert, {"a": "enrolled", "d": "{}"})
    filler = "x" * (MAX_AUDIT_DETAILS_BYTES - len('{"k": ""}'))
    await _sql(migrated, insert, {"a": "enrolled", "d": f'{{"k": "{filler}"}}'})
    for action, details in (
        ("made_up", "{}"),
        ("enrolled", "[]"),
        ("enrolled", f'{{"k": "{filler}x"}}'),
    ):
        with pytest.raises(IntegrityError):
            await _sql(migrated, insert, {"a": action, "d": details})
    rows = await _sql(migrated, "SELECT at IS NOT NULL FROM audit_event")
    assert rows == [(True,), (True,)]


async def test_down_up_cycles_are_clean(migrated: DatabaseSettings) -> None:
    cfg = alembic_config(migrated)
    node = str(new_node_id())
    await _sql(migrated, _NODE, {"n": node})
    for _ in range(2):
        await asyncio.to_thread(command.downgrade, cfg, "0014")
        tables = await _sql(
            migrated,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('join_token', 'audit_event')",
        )
        assert tables == [(0,)]
        columns = await _sql(
            migrated,
            "SELECT count(*) FROM information_schema.columns WHERE "
            "table_name = 'node' AND column_name IN ('cert_serial', "
            "'cert_fingerprint', 'cert_not_after', 'public_key_fingerprint')",
        )
        assert columns == [(0,)]
        assert await _sql(migrated, "SELECT count(*) FROM node") == [(1,)]
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.check, cfg)
