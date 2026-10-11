"""V0.7 G4: migration 0018 on real PostgreSQL (D140, D141).

* ``node`` pending-certificate columns: all four or none, hex shapes, unique
  serial, only next to a current certificate; ``cert_renewed_at`` nullable;
* ``app_user``: login-name shape, unique name, known roles, hash length;
* ``api_token``: 64-hex hash, unique, expiry after creation, a name, deleted
  with its user;
* ``job.owner_user_id`` -> ``app_user`` (``SET NULL``), NULL for existing jobs;
* ``audit_event`` accepts every new action, refuses unknown ones;
* down to 0017 and up again is clean (``alembic check``): the downgrade drops
  every table/column, deletes the new audit rows and records an unused pending
  certificate as ``superseded`` (it stays CA-valid until it expires).
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

from meshforge.common.ids import new_api_token_id, new_node_id, new_user_id
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
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
    "first_seen_at, last_seen_at, session_epoch, cert_serial, cert_fingerprint, "
    "cert_not_after, public_key_fingerprint) VALUES (:n, 'h', 'l', 'p', '{}', "
    "'online', now(), now(), 1, :s, :f, now() + make_interval(days => :d), :k)"
)
_PENDING = (
    "UPDATE node SET pending_cert_serial = :s, pending_cert_fingerprint = :f, "
    "pending_cert_not_after = now() + interval '30 days', "
    "pending_public_key_fingerprint = :k, cert_renewed_at = now() "
    "WHERE node_id = :n"
)
_USER = "INSERT INTO app_user (id, name, role, pw_hash) VALUES (:id, :name, :role, :h)"
_TOKEN = (
    "INSERT INTO api_token (id, user_id, name, token_hash, created_at, expires_at) "
    "VALUES (:id, :u, :name, :h, now(), now() + make_interval(secs => :ttl))"
)
_HASH = "scrypt$32768$8$1$" + "A" * 22 + "$" + "B" * 43


def _node(serial: str | None = "0a") -> dict[str, Any]:
    return {
        "n": str(new_node_id()),
        "d": None if serial is None else 30,
        "s": serial,
        "f": None if serial is None else "c" * 64,
        "k": None if serial is None else "d" * 64,
    }


async def test_pending_certificate_columns(migrated: DatabaseSettings) -> None:
    node = _node()
    await _sql(migrated, _NODE, node)
    assert await _sql(
        migrated,
        "SELECT pending_cert_serial, cert_renewed_at FROM node WHERE node_id = :n",
        {"n": node["n"]},
    ) == [(None, None)]
    ok = {"s": "0b", "f": "e" * 64, "k": "f" * 64, "n": node["n"]}
    await _sql(migrated, _PENDING, ok)
    other = _node("1a")
    await _sql(migrated, _NODE, other)
    for params in (
        {**ok, "n": other["n"]},  # pending serial unique
        {**ok, "n": other["n"], "s": "XY"},
        {**ok, "n": other["n"], "s": "0c", "f": "E" * 64},
        {**ok, "n": other["n"], "s": "0c", "k": "f" * 63},
    ):
        with pytest.raises(IntegrityError):
            await _sql(migrated, _PENDING, params)
    with pytest.raises(IntegrityError):  # half set
        await _sql(
            migrated,
            "UPDATE node SET pending_cert_serial = '0d' WHERE node_id = :n",
            {"n": other["n"]},
        )
    bare = _node(None)
    await _sql(migrated, _NODE, bare)
    with pytest.raises(IntegrityError):  # pending without a current certificate
        await _sql(migrated, _PENDING, {**ok, "s": "0e", "n": bare["n"]})


async def test_users_tokens_and_job_owner(migrated: DatabaseSettings) -> None:
    alice = str(new_user_id())
    await _sql(
        migrated, _USER, {"id": alice, "name": "alice", "role": "admin", "h": _HASH}
    )
    for name, role, pw in (
        ("alice", "user", _HASH),  # name unique
        ("Bob", "user", _HASH),  # lower case only
        ("-bob", "user", _HASH),
        ("bob", "root", _HASH),  # unknown role
        ("bob", "user", "short"),  # not a hash
    ):
        with pytest.raises(IntegrityError):
            await _sql(
                migrated,
                _USER,
                {"id": str(new_user_id()), "name": name, "role": role, "h": pw},
            )
    with pytest.raises(DBAPIError, match="too long"):  # > 64 characters
        await _sql(
            migrated,
            _USER,
            {"id": str(new_user_id()), "name": "b" * 65, "role": "user", "h": _HASH},
        )
    assert await _sql(
        migrated, "SELECT disabled, created_at IS NOT NULL FROM app_user"
    ) == [(False, True)]

    token = {"id": str(new_api_token_id()), "u": alice, "name": "cli", "h": "a" * 64}
    await _sql(migrated, _TOKEN, {**token, "ttl": 3600})
    for params in (
        {**token, "id": str(new_api_token_id()), "ttl": 3600},  # hash unique
        {**token, "id": str(new_api_token_id()), "h": "A" * 64, "ttl": 3600},
        {**token, "id": str(new_api_token_id()), "h": "b" * 64, "ttl": 0},
        {**token, "id": str(new_api_token_id()), "h": "c" * 64, "name": "", "ttl": 9},
        {
            **token,
            "id": str(new_api_token_id()),
            "h": "d" * 64,
            "u": str(new_user_id()),  # no such user
            "ttl": 9,
        },
    ):
        with pytest.raises(IntegrityError):
            await _sql(migrated, _TOKEN, params)

    columns = await _sql(
        migrated,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'job' AND column_name = 'owner_user_id'",
    )
    assert columns == [("YES",)]
    await _sql(migrated, "DELETE FROM app_user WHERE id = :u", {"u": alice})
    assert await _sql(migrated, "SELECT count(*) FROM api_token") == [(0,)]


async def test_audit_actions(migrated: DatabaseSettings) -> None:
    insert = "INSERT INTO audit_event (action, actor, details) VALUES (:a, 't', '{}')"
    for action in (
        "cert_renewed",
        "cert_renew_refused",
        "cert_activated",
        "user_created",
        "user_disabled",
        "user_enabled",
        "password_reset",
        "login",
        "login_failed",
        "token_revoked",
        "legacy_token_used",
        "node_revoked",
    ):
        await _sql(migrated, insert, {"a": action})
    with pytest.raises(IntegrityError):
        await _sql(migrated, insert, {"a": "made_up"})


async def test_down_up_cycles_are_clean(migrated: DatabaseSettings) -> None:
    cfg = alembic_config(migrated)
    for round_ in range(2):
        node = _node(f"2{round_}")
        await _sql(migrated, _NODE, node)
        await _sql(
            migrated,
            _PENDING,
            {"s": f"3{round_}", "f": "e" * 64, "k": "f" * 64, "n": node["n"]},
        )
        await _sql(
            migrated,
            "INSERT INTO audit_event (action, actor, details) VALUES "
            "('cert_renewed', 't', '{}'), ('enrolled', 't', '{}')",
        )
        await asyncio.to_thread(command.downgrade, cfg, "0017")
        assert await _sql(
            migrated,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('app_user', 'api_token')",
        ) == [(0,)]
        assert await _sql(
            migrated,
            "SELECT count(*) FROM information_schema.columns WHERE "
            "(table_name = 'node' AND column_name LIKE 'pending_%') OR "
            "(table_name = 'node' AND column_name = 'cert_renewed_at') OR "
            "(table_name = 'job' AND column_name = 'owner_user_id')",
        ) == [(0,)]
        # The unused pending certificate stays refused after the downgrade.
        assert await _sql(
            migrated,
            "SELECT node_id, reason FROM revoked_cert WHERE serial = :s",
            {"s": f"3{round_}"},
        ) == [(node["n"], "superseded")]
        actions = await _sql(migrated, "SELECT DISTINCT action FROM audit_event")
        assert ("cert_renewed",) not in actions and ("enrolled",) in actions
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.check, cfg)
