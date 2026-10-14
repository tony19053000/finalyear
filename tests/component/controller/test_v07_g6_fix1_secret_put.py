"""V0.7 G6 fix round 1 (D162, T-1): ``SecretStore.put`` is atomic in PostgreSQL.

Two stores on two engines (two controller processes would look the same to
the database - nothing in this process coordinates them):

* 60 concurrent first puts of one name: ``created`` is true **exactly once**,
  one row, one value of those written;
* a burst of new names past the limit: **exactly** 256 succeed, the rest are
  refused typed (``resource_busy``), 256 rows - not one more;
* at the limit an overwrite still works and reports ``created`` false; a
  delete frees one place for exactly one of many concurrent new names;
* one owner's burst does not block or count against another owner;
* the cap pieces of the same round: ``check_present`` / ``resolve`` report
  ``secret_bytes_over_limit`` from the stored sizes.
"""

from __future__ import annotations

import asyncio
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
from meshforge.controller.secret_store import (
    MAX_SECRETS_PER_USER,
    SecretLimitError,
    SecretsTooLargeError,
    SecretStore,
)
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

PW = "scrypt$32768$8$1$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA=="


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
    upgrade_to_head(pg_settings)
    return pg_settings


@pytest_asyncio.fixture
async def env(
    migrated: DatabaseSettings, tmp_path: Path
) -> AsyncIterator[tuple[Any, list[SecretStore], str, str]]:
    engines = [create_engine(migrated), create_engine(migrated)]
    users = UserStore(engines[0])
    alice = await users.create_user(
        "alice", pw_hash=PW, role=UserRole.USER, actor="test"
    )
    bob = await users.create_user("bob", pw_hash=PW, role=UserRole.USER, actor="test")
    key = tmp_path / "controller" / "secrets.key"
    stores = [SecretStore(engine, key) for engine in engines]
    await stores[0].box()  # the key exists before the second "process" reads it
    try:
        yield engines[0], stores, alice.id, bob.id
    finally:
        for engine in engines:
            await engine.dispose()


async def _count(engine: Any, owner: str) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM secret WHERE owner_user_id = :o"), {"o": owner}
        )
        return int(result.scalar_one())


@pytest.mark.security
async def test_created_is_true_exactly_once_across_two_stores(env: Any) -> None:
    engine, stores, alice, _bob = env
    for round_ in range(4):
        name = f"RACE_{round_}"
        results = await asyncio.gather(
            *[
                stores[i % 2].put(alice, name, f"value-{i:04d}".encode(), actor="t")
                for i in range(60)
            ]
        )
        assert results.count(True) == 1, results
        resolved = (await stores[0].resolve(alice, [name]))[name]
        assert resolved in {f"value-{i:04d}".encode() for i in range(60)}
    assert await _count(engine, alice) == 4


@pytest.mark.security
async def test_the_limit_is_exact_under_a_burst_from_two_stores(env: Any) -> None:
    engine, stores, alice, bob = env
    total = MAX_SECRETS_PER_USER + 144
    results = await asyncio.gather(
        *[
            stores[i % 2].put(alice, f"N{i}", b"value-1234", actor="t")
            for i in range(total)
        ],
        # Another owner at the same time: neither blocked nor counted.
        *[stores[i % 2].put(bob, f"B{i}", b"value-1234", actor="t") for i in range(20)],
        return_exceptions=True,
    )
    mine, others = results[:total], results[total:]
    assert others == [True] * 20
    refused = [r for r in mine if isinstance(r, SecretLimitError)]
    assert [r for r in mine if isinstance(r, Exception) and r not in refused] == []
    assert mine.count(True) == MAX_SECRETS_PER_USER
    assert len(refused) == total - MAX_SECRETS_PER_USER
    assert refused[0].failure_type is FailureType.RESOURCE_BUSY
    assert await _count(engine, alice) == MAX_SECRETS_PER_USER
    assert await _count(engine, bob) == 20
    # At the limit: an overwrite works and is not "created".
    kept = next(i for i, r in enumerate(mine) if r is True)
    assert await stores[1].put(alice, f"N{kept}", b"value-5678", actor="t") is False
    # One place freed: exactly one of many concurrent new names gets it.
    await stores[0].delete(alice, f"N{kept}", actor="t")
    again = await asyncio.gather(
        *[
            stores[i % 2].put(alice, f"LATE{i}", b"value-1234", actor="t")
            for i in range(30)
        ],
        return_exceptions=True,
    )
    assert again.count(True) == 1
    assert sum(isinstance(r, SecretLimitError) for r in again) == 29
    assert await _count(engine, alice) == MAX_SECRETS_PER_USER


async def test_the_job_cap_reads_the_stored_sizes(env: Any) -> None:
    _engine, stores, alice, _bob = env
    store = stores[0]
    await store.put(alice, "A", b"x" * 60, actor="t")
    await store.put(alice, "B", b"y" * 41, actor="t")
    await store.check_present(alice, ["A", "B"], max_total_bytes=101)
    assert set(await store.resolve(alice, ["A", "B"], max_total_bytes=101)) == {
        "A",
        "B",
    }
    for call in (store.check_present, store.resolve):
        with pytest.raises(SecretsTooLargeError) as exc:
            await call(alice, ["a", "B"], max_total_bytes=100)
        assert (exc.value.total_bytes, exc.value.max_bytes) == (101, 100)
        assert exc.value.failure_type is FailureType.INVALID_INPUT
        assert str(exc.value).startswith("secret_bytes_over_limit:")
        assert "xxxx" not in str(exc.value)
    # Only the named secrets count; no cap = no check.
    await store.check_present(alice, ["A"], max_total_bytes=60)
    await store.check_present(alice, ["A", "B"])
