"""V0.7 G7 fix round 1 (D167, weak row LF-I6): the database alone leaks no
secret - the master key is not in it, and without the key file the rows do
not decrypt.

``test_v07_g6_secret_store.py::test_the_database_never_holds_the_value``
checks the *value* is absent. LF-I6's claim is about the *key*: here every
row of every table (``row_to_json``) and every ``bytea`` cell is searched for
the master key (raw, hex, base64), and a store given the same database but
another key file (a stolen dump without ``secrets.key``) cannot resolve the
secret: ``SecretIntegrityError``, never the value.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import pytest_asyncio
from sqlalchemy import text

from meshforge.controller.auth.users import UserStore
from meshforge.controller.secret_box import SecretIntegrityError
from meshforge.controller.secret_store import SecretStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

VALUE = b"lf-i6-secret-value-\x00\xff-tail"
PW = "scrypt$32768$8$1$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA=="


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
    upgrade_to_head(pg_settings)  # sync: Alembic drives its own event loop
    return pg_settings


@pytest_asyncio.fixture
async def engine(migrated: DatabaseSettings) -> AsyncIterator[Any]:
    engine = create_engine(migrated)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _rows(engine: Any, statement: str) -> list[Any]:
    async with engine.begin() as conn:
        return list((await conn.execute(text(statement))).all())


async def test_the_master_key_is_not_in_the_database_and_rows_need_it(
    engine: Any, tmp_path: Path
) -> None:
    users = UserStore(engine)
    alice = await users.create_user(
        "alice", pw_hash=PW, role=UserRole.USER, actor="test"
    )
    key_path = tmp_path / "controller" / "secrets.key"
    store = SecretStore(engine, key_path)
    for name in ("ONE", "TWO", "THREE"):
        await store.put(alice.id, name, VALUE + name.encode(), actor="alice")
    assert await store.resolve(alice.id, ["ONE"]) == {"ONE": VALUE + b"ONE"}
    key = key_path.read_bytes()
    assert len(key) == 32
    forms = {
        "raw": key,
        "hex": key.hex().encode(),
        "base64": base64.b64encode(key).rstrip(b"="),
        "base64url": base64.urlsafe_b64encode(key).rstrip(b"="),
    }

    tables = [
        str(name)
        for (name,) in await _rows(
            engine,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
        )
    ]
    assert "secret" in tables
    dump = b""
    for table in tables:
        for (row,) in await _rows(
            engine,
            f'SELECT row_to_json(t)::text FROM "{table}" t',  # noqa: S608 - names from information_schema
        ):
            dump += row.encode() + b"\n"
    columns = await _rows(
        engine,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = 'bytea'",
    )
    assert ("secret", "ciphertext") in {(str(t), str(c)) for t, c in columns}
    for table, column in columns:
        query = f'SELECT "{column}" FROM "{table}"'  # noqa: S608 - names from information_schema
        for (cell,) in await _rows(engine, query):
            if cell is not None:
                dump += bytes(cell) + b"\n"
    found = [form for form, needle in forms.items() if needle in dump]
    assert found == [], found
    assert VALUE not in dump

    # A stolen dump without secrets.key: another key cannot open the rows.
    thief = SecretStore(engine, tmp_path / "elsewhere" / "secrets.key")
    with pytest.raises(SecretIntegrityError):
        await thief.resolve(alice.id, ["ONE"])
