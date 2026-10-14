"""V0.7 G6 deliverable 1 (D144): the secret store on real PostgreSQL.

* put/list/delete/resolve per owner; one name per owner (a second put replaces);
* the database never holds the value: every column of the row and the whole
  table dump are searched for it (and for its hex);
* owner scoping: another user resolves the name as ``secret_missing``; a row
  copied to another owner or renamed in SQL fails to decrypt ``integrity_error``;
* audit rows name the secret, never carry the value;
* the user's secrets go with the user (``CASCADE``); migration 0019 down/up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import pytest_asyncio
from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.controller.auth.users import UserStore
from meshforge.controller.secret_box import SecretIntegrityError
from meshforge.controller.secret_store import (
    SecretNotFoundError,
    SecretsMissingError,
    SecretStore,
)
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

VALUE = b"s3cr3t-value-for-the-recorder-\x00\xff-tail"
PW = "scrypt$32768$8$1$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA=="


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
    upgrade_to_head(pg_settings)  # sync: Alembic drives its own event loop
    return pg_settings


@pytest_asyncio.fixture
async def env(
    migrated: DatabaseSettings, tmp_path: Path
) -> AsyncIterator[tuple[Any, SecretStore, str, str]]:
    engine = create_engine(migrated)
    users = UserStore(engine)
    alice = await users.create_user(
        "alice", pw_hash=PW, role=UserRole.USER, actor="test"
    )
    bob = await users.create_user("bob", pw_hash=PW, role=UserRole.USER, actor="test")
    store = SecretStore(engine, tmp_path / "controller" / "secrets.key")
    try:
        yield engine, store, alice.id, bob.id
    finally:
        await engine.dispose()


async def _sql(engine: Any, statement: str, **params: Any) -> list[Any]:
    async with engine.begin() as conn:
        result = await conn.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


async def test_put_list_resolve_delete(env: Any) -> None:
    _engine, store, alice, bob = env
    assert await store.put(alice, "api_key", VALUE, actor="alice") is True
    assert await store.put(alice, "API_KEY", VALUE + b"2", actor="alice") is False
    await store.put(bob, "API_KEY", b"bobs-value", actor="bob")
    listed = await store.list(alice)
    assert [i.name for i in listed] == ["API_KEY"]
    assert listed[0].updated_at >= listed[0].created_at
    assert await store.resolve(alice, ["api_key"]) == {"API_KEY": VALUE + b"2"}
    assert await store.resolve(bob, ["API_KEY"]) == {"API_KEY": b"bobs-value"}
    await store.delete(alice, "API_KEY", actor="alice")
    assert await store.list(alice) == []
    with pytest.raises(SecretNotFoundError):
        await store.delete(alice, "API_KEY", actor="alice")
    assert await store.resolve(bob, ["API_KEY"]) == {"API_KEY": b"bobs-value"}


@pytest.mark.security
async def test_the_database_never_holds_the_value(env: Any) -> None:
    engine, store, alice, _bob = env
    await store.put(alice, "TOKEN", VALUE, actor="alice")
    (row,) = await _sql(engine, "SELECT * FROM secret")
    for column in row:
        raw = column if isinstance(column, bytes) else str(column).encode()
        assert VALUE not in raw and VALUE.hex().encode() not in raw
    dump = await _sql(
        engine,
        "SELECT row_to_json(s)::text FROM secret s UNION ALL "
        "SELECT row_to_json(a)::text FROM audit_event a",
    )
    blob = " ".join(r[0] for r in dump)
    assert VALUE.hex() not in blob and "s3cr3t-value" not in blob
    actions = await _sql(engine, "SELECT action, details::text FROM audit_event")
    assert any(a == "secret_set" and '"TOKEN"' in d for a, d in actions), actions


@pytest.mark.security
async def test_owner_scoping(env: Any) -> None:
    _engine, store, alice, bob = env
    await store.put(alice, "ONLY_ALICE", VALUE, actor="alice")
    with pytest.raises(SecretsMissingError) as exc:
        await store.resolve(bob, ["ONLY_ALICE", "ALSO_MISSING"])
    assert exc.value.names == ("ALSO_MISSING", "ONLY_ALICE")
    assert "secret_missing:ONLY_ALICE" in str(exc.value)
    with pytest.raises(SecretsMissingError):
        await store.resolve(None, ["ONLY_ALICE"])  # a job without an owner
    assert await store.resolve(alice, []) == {}


@pytest.mark.security
@pytest.mark.parametrize("tamper", ["owner", "name"])
async def test_a_copied_or_renamed_row_does_not_decrypt(env: Any, tamper: str) -> None:
    engine, store, alice, bob = env
    await store.put(alice, "SRC", VALUE, actor="alice")
    if tamper == "owner":
        await _sql(
            engine,
            "INSERT INTO secret (owner_user_id, name, ciphertext, nonce, key_id) "
            "SELECT :bob, name, ciphertext, nonce, key_id FROM secret "
            "WHERE owner_user_id = :alice",
            bob=bob,
            alice=alice,
        )
        owner, name = bob, "SRC"
    else:
        await _sql(engine, "UPDATE secret SET name = 'DST' WHERE name = 'SRC'")
        owner, name = alice, "DST"
    with pytest.raises(SecretIntegrityError) as exc:
        await store.resolve(owner, [name])
    assert exc.value.failure_type is FailureType.INTEGRITY_ERROR


async def test_secrets_go_with_their_user(env: Any) -> None:
    engine, store, alice, _bob = env
    await store.put(alice, "GONE", VALUE, actor="alice")
    await _sql(engine, "DELETE FROM app_user WHERE id = :a", a=alice)
    assert await _sql(engine, "SELECT count(*) FROM secret") == [(0,)]


async def test_the_table_refuses_plain_shapes(env: Any) -> None:
    engine, _store, alice, _bob = env
    with pytest.raises(Exception, match="ck_secret"):
        await _sql(
            engine,
            "INSERT INTO secret (owner_user_id, name, ciphertext, nonce, key_id) "
            "VALUES (:a, 'lower', '\\x00'::bytea, '\\x00'::bytea, 'zz')",
            a=alice,
        )
