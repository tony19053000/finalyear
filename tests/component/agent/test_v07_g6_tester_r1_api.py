"""V0.7 G6 tester r1: attack ``/v1/secrets`` on a real mTLS controller + PG.

Criteria (brief 5f deliverables 1-2): user A can never read, list, overwrite or
delete user B's secrets; an admin cannot read values; no endpoint, error body,
audit row or OpenAPI route ever returns a value; name tricks and value bounds;
the database holds no plaintext (every table's rows + the Postgres data files);
concurrent writers; the per-user limit.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.secret_names import MAX_SECRET_BYTES
from meshforge.controller.secret_store import MAX_SECRETS_PER_USER, SecretStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(300)]

PW = {"alice": "alice-password-1", "bob": "bob-password-1", "admin": "admin-pw-1"}
ALICE_VALUE = b"alice-VALUE-\x01\x02-r1-7f3a9c"


def forms(value: bytes) -> list[bytes]:
    return [
        value,
        value.hex().encode(),
        value.hex().upper().encode(),
        base64.b64encode(value),
        base64.b64encode(value).rstrip(b"="),
        base64.urlsafe_b64encode(value),
        value.decode("latin-1").encode("utf-8"),
    ]


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


class Bodies:
    def __init__(self) -> None:
        self.items: list[bytes] = []

    async def hook(self, response: httpx.Response) -> None:
        await response.aread()
        self.items.append(
            response.content + b"\n" + str(dict(response.headers)).encode()
        )

    def joined(self) -> bytes:
        return b"\n".join(self.items)


@pytest_asyncio.fixture
async def world(
    engine: AsyncEngine, runtime: ControllerRuntime
) -> AsyncIterator[tuple[dict[str, httpx.AsyncClient], Bodies, dict[str, str]]]:
    ids: dict[str, str] = {}
    for name, pw in PW.items():
        user = await create_user(engine, name, pw, admin=name == "admin")
        ids[name] = user
    base = f"https://127.0.0.1:{runtime.http_port}"
    bodies = Bodies()
    clients: dict[str, httpx.AsyncClient] = {}
    async with httpx.AsyncClient(base_url=base, timeout=60) as anon:
        for name, pw in PW.items():
            token = await login(anon, name, pw)
            clients[name] = httpx.AsyncClient(
                base_url=base,
                headers=bearer(token),
                timeout=60,
                event_hooks={"response": [bodies.hook]},
            )
    try:
        yield clients, bodies, ids
    finally:
        for c in clients.values():
            await c.aclose()


async def _owner_ids(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT name, id FROM app_user"))
        return {r[0]: r[1] for r in rows}


async def test_user_isolation_and_admin_cannot_read(
    world: Any, engine: AsyncEngine, runtime: ControllerRuntime
) -> None:
    clients, bodies, _ = world
    alice, bob, admin = clients["alice"], clients["bob"], clients["admin"]
    ids = await _owner_ids(engine)
    assert (await alice.put("/v1/secrets/SHARED", content=ALICE_VALUE)).is_success
    # Bob stores the SAME name: his own row, Alice's value must be untouched.
    assert (await bob.put("/v1/secrets/SHARED", content=b"bob-own-value-1")).is_success
    store = SecretStore(engine, runtime.settings.secrets_key_path)
    assert (await store.resolve(ids["alice"], ["SHARED"])) == {"SHARED": ALICE_VALUE}
    assert (await store.resolve(ids["bob"], ["SHARED"])) == {
        "SHARED": b"bob-own-value-1"
    }
    # Bob deleting "his" SHARED never touches Alice's.
    assert (await bob.delete("/v1/secrets/SHARED")).is_success
    assert (await store.resolve(ids["alice"], ["SHARED"])) == {"SHARED": ALICE_VALUE}
    # A name only Alice has: Bob/admin delete -> 404 and nothing changes.
    assert (await alice.put("/v1/secrets/ONLY_A", content=ALICE_VALUE)).is_success
    for other in (bob, admin):
        assert (await other.delete("/v1/secrets/ONLY_A")).status_code == 404
        listed = (await other.get("/v1/secrets")).json()
        assert "ONLY_A" not in json.dumps(listed)
    assert {s["name"] for s in (await alice.get("/v1/secrets")).json()["secrets"]} == {
        "SHARED",
        "ONLY_A",
    }
    # No value-returning verb anywhere on the route family.
    for client in (alice, bob, admin):
        for path in ("/v1/secrets/ONLY_A", "/v1/secrets/ONLY_A/value", "/v1/secrets/"):
            for method in ("GET", "HEAD", "POST", "PATCH"):
                r = await client.request(method, path)
                assert r.status_code in (301, 307, 404, 405, 401), (method, path, r)
    # Query-string tricks to reach another user's data.
    r = await admin.get("/v1/secrets", params={"all": "true", "user": "alice"})
    assert "ONLY_A" not in r.text
    r = await admin.get("/v1/secrets", params={"owner_user_id": ids["alice"]})
    assert "ONLY_A" not in r.text
    r = await admin.get("/v1/secrets", headers={"x-user": "alice"})
    assert "ONLY_A" not in r.text
    for form in forms(ALICE_VALUE):
        assert form not in bodies.joined()


async def test_name_tricks_over_http_are_4xx_never_5xx_and_store_nothing(
    world: Any, engine: AsyncEngine
) -> None:
    clients, bodies, _ = world
    alice = clients["alice"]
    tricks = [
        "%E2%84%AAEY",  # Kelvin sign
        "%EF%BC%AB%EF%BC%A5%EF%BC%B9",  # full-width
        "A%00B",
        "%00",
        "..",
        "%2e%2e",
        "A%2FB",
        "A%5CB",
        "A%20B",
        "1ABC",
        "A" * 65,
        "A" * 5000,
        "%F0%9F%94%91",
        "A;B",
        "A%3FB",
        "A%23B",
    ]
    for trick in tricks:
        for method in ("PUT", "DELETE"):
            r = await alice.request(
                method, f"/v1/secrets/{trick}", content=b"value-1234"
            )
            assert 400 <= r.status_code < 500, (method, trick, r.status_code, r.text)
        for form in forms(b"value-1234"):
            assert form not in r.content
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM secret"))).scalar_one()
    assert n == 0
    # Normalisation: lower case, surrounding space and an encoded space collapse
    # to the same single row.
    for spelled in ("db_pass", "DB_PASS", "Db_Pass", "%20db_pass%20", "DB_PASS%0A"):
        assert (
            await alice.put(f"/v1/secrets/{spelled}", content=b"value-1234")
        ).is_success
    assert [s["name"] for s in (await alice.get("/v1/secrets")).json()["secrets"]] == [
        "DB_PASS"
    ]
    assert bodies.joined().count(b"Traceback") == 0


async def test_value_bounds_binary_chunked_and_no_value_in_errors(world: Any) -> None:
    clients, bodies, _ = world
    alice = clients["alice"]
    cases = {
        0: 400,
        1: 400,
        3: 400,
        4: 200,
        MAX_SECRET_BYTES: 200,
        MAX_SECRET_BYTES + 1: 400,
        MAX_SECRET_BYTES * 20: 400,
    }
    for size, code in cases.items():
        r = await alice.put("/v1/secrets/B", content=b"x" * size)
        assert r.status_code == code, (size, r.status_code, r.text[:200])

    # chunked body without a content-length, oversize: must be cut off, not buffered
    async def gen() -> Any:
        for _ in range(64):
            yield b"y" * 4096

    r = await alice.put("/v1/secrets/CHUNKED", content=gen())
    assert r.status_code == 400, (r.status_code, r.text[:200])

    # chunked, small -> accepted
    async def small() -> Any:
        yield b"abc"
        yield b"defgh"

    assert (await alice.put("/v1/secrets/CHUNKED", content=small())).is_success
    # binary + trailing newline + CRLF are stored byte for byte (no stripping)
    raw = b"\x00\xff\xfe\n\r\n  trailing \n\n"
    assert (await alice.put("/v1/secrets/RAW", content=raw)).is_success
    # content types are irrelevant: a JSON body is just bytes, never parsed
    r = await alice.put(
        "/v1/secrets/JSONISH",
        content=b'{"value": "abcd"}',
        headers={"content-type": "application/json"},
    )
    assert r.is_success
    assert b"Traceback" not in bodies.joined()


async def test_concurrent_writers_never_500_and_end_with_one_row(
    world: Any,
) -> None:
    clients, _, _ = world
    alice = clients["alice"]
    results = await asyncio.gather(
        *[
            alice.put("/v1/secrets/RACE", content=f"value-{i}-aaaa".encode())
            for i in range(40)
        ]
    )
    assert {r.status_code for r in results} == {200}, [r.status_code for r in results]
    assert len((await alice.get("/v1/secrets")).json()["secrets"]) == 1
    dels = await asyncio.gather(*[alice.delete("/v1/secrets/RACE") for _ in range(10)])
    assert sorted(r.status_code for r in dels) == [200] + [404] * 9


@pytest.mark.xfail(
    strict=True,
    reason="T-1 (minor): SecretStore.put counts existing rows and inserts in "
    "separate statements under READ COMMITTED, so concurrent first-time PUTs "
    "of one name all report created=true, and concurrent PUTs of different "
    "new names can exceed the 256-per-user limit",
)
async def test_created_flag_is_true_exactly_once_under_concurrency(
    world: Any,
) -> None:
    clients, _, _ = world
    alice = clients["alice"]
    for attempt in range(5):
        name = f"RACE{attempt}"
        results = await asyncio.gather(
            *[
                alice.put(f"/v1/secrets/{name}", content=b"value-1234")
                for _ in range(40)
            ]
        )
        created = [r.json()["created"] for r in results]
        assert created.count(True) == 1, created


@pytest.mark.xfail(
    strict=True,
    reason="T-1 (minor): the per-user limit is checked, then inserted, in "
    "separate transactions' statements: parallel new names can overshoot "
    "MAX_SECRETS_PER_USER",
)
async def test_the_per_user_limit_holds_under_a_burst_of_new_names(
    world: Any, engine: AsyncEngine, runtime: ControllerRuntime
) -> None:
    ids = await _owner_ids(engine)
    store = SecretStore(engine, runtime.settings.secrets_key_path)
    total = MAX_SECRETS_PER_USER + 300
    results = await asyncio.gather(
        *[
            store.put(ids["bob"], f"B{i}", b"value-1234", actor="bob")
            for i in range(total)
        ],
        return_exceptions=True,
    )
    assert not [
        r for r in results if isinstance(r, Exception) and "at most" not in str(r)
    ]
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM secret WHERE owner_user_id = :o"),
                {"o": ids["bob"]},
            )
        ).scalar_one()
    assert n <= MAX_SECRETS_PER_USER, n


async def test_per_user_limit_is_enforced_and_overwrite_stays_possible(
    world: Any,
) -> None:
    clients, _, _ = world
    bob = clients["bob"]
    for start in range(0, MAX_SECRETS_PER_USER, 32):
        batch = await asyncio.gather(
            *[
                bob.put(f"/v1/secrets/S{i}", content=b"value-1234")
                for i in range(start, min(start + 32, MAX_SECRETS_PER_USER))
            ]
        )
        assert all(r.is_success for r in batch)
    over = await bob.put("/v1/secrets/ONE_MORE", content=b"value-1234")
    assert over.status_code == 409, over.text
    assert (await bob.put("/v1/secrets/S0", content=b"value-5678")).is_success
    assert (await bob.get("/v1/secrets")).json()["count"] == MAX_SECRETS_PER_USER
    assert (await bob.delete("/v1/secrets/S1")).is_success
    assert (await bob.put("/v1/secrets/ONE_MORE", content=b"value-1234")).is_success
    # Alice is unaffected by Bob's limit.
    assert (
        await clients["alice"].put("/v1/secrets/X1", content=b"value-1234")
    ).is_success


async def test_openapi_exposes_no_value_returning_secret_route(
    world: Any, runtime: ControllerRuntime
) -> None:
    clients, _, _ = world
    r = await clients["alice"].get("/openapi.json")
    if r.status_code != 200:
        pytest.skip("openapi disabled")
    spec = r.json()
    for path, ops in spec["paths"].items():
        if "secret" in path:
            assert set(ops) <= {"put", "get", "delete"}, (path, set(ops))
            if path.endswith("{name}"):
                assert set(ops) == {"put", "delete"}, (
                    "a by-name GET could return a value"
                )
    schema_text = json.dumps(spec)
    assert '"value"' not in json.dumps(
        {k: v for k, v in spec["paths"].items() if "secret" in k}
    )
    assert "ciphertext" not in schema_text


async def test_audit_rows_and_db_hold_no_value_anywhere(
    world: Any, engine: AsyncEngine, pg_settings: DatabaseSettings
) -> None:
    clients, bodies, _ = world
    alice = clients["alice"]
    value = b"AUDIT-PLAINTEXT-needle-" + base64.b32encode(b"r1-xyz")
    assert (await alice.put("/v1/secrets/AUD", content=value)).is_success
    assert (await alice.put("/v1/secrets/AUD", content=value + b"2")).is_success
    assert (await alice.delete("/v1/secrets/AUD")).is_success
    assert (await alice.put("/v1/secrets/AUD", content=value)).is_success
    async with engine.connect() as conn:
        tables = [
            r[0]
            for r in await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ]
        assert "secret" in tables and "audit_event" in tables
        dump = b""
        for table in tables:
            rows = await conn.execute(text(f'SELECT t::text FROM "{table}" t'))  # noqa: S608
            dump += "\n".join(r[0] for r in rows).encode()
        actions = [
            r[0]
            for r in await conn.execute(text("SELECT action::text FROM audit_event"))
        ]
        await conn.execute(text("CHECKPOINT"))
    assert "secret_set" in actions and "secret_deleted" in actions
    for form in [*forms(value), b"AUDIT-PLAINTEXT"]:
        assert form not in dump, form
    for form in forms(value):
        assert form not in bodies.joined()
    # And in the Postgres data files themselves (heap, WAL): grep the container.
    found = subprocess.run(  # noqa: ASYNC221
        ["docker", "ps", "--format", "{{.ID}} {{.Ports}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    cid = next(
        (
            line.split()[0]
            for line in found.splitlines()
            if f":{pg_settings.port}->" in line
        ),
        None,
    )
    if cid is None:
        pytest.skip("postgres container not found")
    grep = subprocess.run(  # noqa: ASYNC221
        [
            "docker",
            "exec",
            cid,
            "sh",
            "-c",
            "grep -rlaF -e AUDIT-PLAINTEXT /var/lib/postgresql/data || true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert grep.stdout.strip() == "", grep.stdout
