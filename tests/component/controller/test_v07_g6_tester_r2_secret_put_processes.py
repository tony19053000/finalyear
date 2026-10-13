# ruff: noqa: E501, RUF059  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (T-1): `SecretStore.put` across TWO OS PROCESSES on real
PostgreSQL (separate interpreters, separate engines, separate pools - what two
controller processes would be).

The limit is exact at 255/256/257, overwrite at the limit is allowed, delete +
re-create works, two owners hitting each other's rows in opposite order do not
deadlock, and a refused put leaves no advisory lock or open transaction behind.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import pytest_asyncio
from sqlalchemy import text

from meshforge.controller.auth.users import UserStore
from meshforge.controller.secret_store import MAX_SECRETS_PER_USER, SecretStore
from meshforge.controller.store.engine import build_url, create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(400)]

PW = "scrypt$32768$8$1$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA=="

WORKER = textwrap.dedent(
    """
    import asyncio, json, sys
    from pathlib import Path
    from meshforge.controller.secret_store import SecretStore
    from meshforge.controller.store.engine import create_engine
    from meshforge.controller.store.settings import DatabaseSettings

    async def main():
        spec = json.loads(sys.argv[1])
        engine = create_engine(DatabaseSettings(url=spec["url"]))
        store = SecretStore(engine, Path(spec["key"]))
        async def one(op):
            kind, owner, name = op
            try:
                if kind == "put":
                    return ["put", owner, name, await store.put(owner, name, b"value-" + name.encode(), actor="w")]
                await store.delete(owner, name, actor="w")
                return ["del", owner, name, True]
            except BaseException as exc:
                return [kind, owner, name, type(exc).__name__]
        res = await asyncio.gather(*[one(op) for op in spec["ops"]])
        print("RESULT" + json.dumps(res))
        await engine.dispose()

    asyncio.run(main())
    """
)


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
    upgrade_to_head(pg_settings)
    return pg_settings


@pytest_asyncio.fixture
async def env(
    migrated: DatabaseSettings, tmp_path: Path
) -> AsyncIterator[tuple[Any, SecretStore, str, str, str, str]]:
    engine = create_engine(migrated)
    users = UserStore(engine)
    alice = await users.create_user("alice", pw_hash=PW, role=UserRole.USER, actor="t")
    bob = await users.create_user("bob", pw_hash=PW, role=UserRole.USER, actor="t")
    key = tmp_path / "controller" / "secrets.key"
    store = SecretStore(engine, key)
    await store.box()
    url = build_url(migrated).render_as_string(hide_password=False)
    try:
        yield engine, store, alice.id, bob.id, url, str(key)
    finally:
        await engine.dispose()


async def _spawn(url: str, key: str, ops: list[tuple[str, str, str]]) -> asyncio.Task:  # type: ignore[type-arg]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        WORKER,
        json.dumps({"url": url, "key": key, "ops": ops}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def wait() -> list[list[Any]]:
        out, err = await asyncio.wait_for(proc.communicate(), 240)
        assert proc.returncode == 0, err.decode()[-2000:]
        line = next(x for x in out.decode().splitlines() if x.startswith("RESULT"))
        return json.loads(line[len("RESULT") :])  # type: ignore[no-any-return]

    return asyncio.create_task(wait())


async def _count(engine: Any, owner: str) -> int:
    async with engine.connect() as conn:
        r = await conn.execute(
            text("SELECT count(*) FROM secret WHERE owner_user_id=:o"), {"o": owner}
        )
        return int(r.scalar_one())


async def _fill(store: SecretStore, owner: str, n: int) -> None:
    for i in range(n):
        await store.put(owner, f"FILL{i}", b"value-fill", actor="t")


async def _no_locks_left(engine: Any) -> None:
    async with engine.connect() as conn:
        locks = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a "
                    "ON a.pid=l.pid WHERE l.locktype='advisory' AND l.granted "
                    "AND a.pid <> pg_backend_pid()"
                )
            )
        ).scalar_one()
        idle = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE state LIKE "
                    "'idle in transaction%' AND pid <> pg_backend_pid()"
                )
            )
        ).scalar_one()
    assert (locks, idle) == (0, 0)


@pytest.mark.parametrize("start", [MAX_SECRETS_PER_USER - 1, MAX_SECRETS_PER_USER, 0])
async def test_the_limit_is_exact_across_two_processes(env: Any, start: int) -> None:
    engine, store, alice, _bob, url, key = env
    await _fill(store, alice, start)
    ops_a = [("put", alice, f"A{i}") for i in range(40)]
    ops_b = [("put", alice, f"B{i}") for i in range(40)]
    ta, tb = await asyncio.gather(_spawn(url, key, ops_a), _spawn(url, key, ops_b))
    res = [*await ta, *await tb]
    created = [r for r in res if r[3] is True]
    refused = [r for r in res if r[3] == "SecretLimitError"]
    others = [r for r in res if r[3] not in (True, "SecretLimitError")]
    assert others == [], others
    assert len(created) == min(80, MAX_SECRETS_PER_USER - start)
    assert len(created) + len(refused) == 80
    assert await _count(engine, alice) == min(MAX_SECRETS_PER_USER, start + 80)
    await _no_locks_left(engine)


async def test_overwrite_delete_recreate_at_the_limit_across_processes(
    env: Any,
) -> None:
    engine, store, alice, _bob, url, key = env
    await _fill(store, alice, MAX_SECRETS_PER_USER)
    # both processes overwrite the same 20 names concurrently: never "created"
    over = [("put", alice, f"FILL{i}") for i in range(20)]
    ta, tb = await asyncio.gather(_spawn(url, key, over), _spawn(url, key, over))
    res = [*await ta, *await tb]
    assert all(r[3] is False for r in res), res
    # delete 3 names (process A) while process B races 10 new names + re-creates
    dels = [("del", alice, f"FILL{i}") for i in range(100, 103)]
    news = [("put", alice, f"NEW{i}") for i in range(10)] + [
        ("put", alice, f"FILL{i}") for i in range(100, 103)
    ]
    ta, tb = await asyncio.gather(_spawn(url, key, dels), _spawn(url, key, news))
    ra, rb = await ta, await tb
    assert all(r[3] is True for r in ra), ra
    errs = [r for r in rb if r[3] not in (True, False, "SecretLimitError")]
    assert errs == [], errs
    n = await _count(engine, alice)
    assert n <= MAX_SECRETS_PER_USER
    # whatever interleaving: stored count = 256 - 3 + (#created)
    created = sum(1 for r in rb if r[3] is True)
    assert n == MAX_SECRETS_PER_USER - 3 + created
    assert created <= 3
    await _no_locks_left(engine)


async def test_two_owners_in_opposite_order_do_not_deadlock(env: Any) -> None:
    engine, _store, alice, bob, url, key = env
    ops1 = [("put", o, f"X{i}") for i in range(40) for o in (alice, bob)]
    ops2 = [("put", o, f"Y{i}") for i in range(40) for o in (bob, alice)]
    ta, tb = await asyncio.gather(_spawn(url, key, ops1), _spawn(url, key, ops2))
    res = [*await asyncio.wait_for(ta, 200), *await asyncio.wait_for(tb, 200)]
    assert all(r[3] is True for r in res), [r for r in res if r[3] is not True]
    assert await _count(engine, alice) == 80 and await _count(engine, bob) == 80
    await _no_locks_left(engine)


async def test_a_refused_or_failed_put_holds_no_lock_afterwards(env: Any) -> None:
    engine, store, alice, bob, url, key = env
    await _fill(store, alice, MAX_SECRETS_PER_USER)
    from meshforge.controller.secret_store import SecretLimitError

    for i in range(5):
        with pytest.raises(SecretLimitError):
            await store.put(alice, f"OVER{i}", b"value-1234", actor="t")
    # an invalid value fails before/inside the transaction: also no lock left
    with pytest.raises(Exception):  # noqa: B017
        await store.put(alice, "BAD", b"x", actor="t")  # shorter than the minimum
    with pytest.raises(Exception):  # noqa: B017
        await store.put(alice, "bad name!", b"value-1234", actor="t")
    await _no_locks_left(engine)
    # the same owner can still write (overwrite) at once, and a delete frees it
    assert (
        await asyncio.wait_for(store.put(alice, "FILL0", b"value-2222", actor="t"), 10)
        is False
    )
    await asyncio.wait_for(store.delete(alice, "FILL1", actor="t"), 10)
    assert (
        await asyncio.wait_for(store.put(alice, "FILL1", b"value-3333", actor="t"), 10)
        is True
    )
    assert await store.put(bob, "B1", b"value-1234", actor="t") is True
