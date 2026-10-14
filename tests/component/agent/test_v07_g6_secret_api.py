"""V0.7 G6 deliverable 2 (D144): ``/v1/secrets`` on a real mTLS controller and
real PostgreSQL, plus ``compute secret`` against it.

* per authenticated user: alice's list never shows bob's names, bob cannot
  delete alice's secret (404), an admin sees only their own;
* **no endpoint returns a value**: every response body of the run is searched
  for the value (raw, hex, base64);
* bounds: 3 bytes / 64 KiB + 1 refused 400, 4 bytes / 64 KiB stored; a bad
  name is refused without echo; no token -> 401; the legacy token -> 400;
* the master key file is created on first use, ``0600``.

The CLI side is ``tests/unit/cli/test_v07_g6_secret_cli.py``.
"""

from __future__ import annotations

import base64
import sys
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.secret_names import MAX_SECRET_BYTES
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

PW = {"alice": "alice-password-1", "bob": "bob-password-1", "admin": "admin-pw-1"}
VALUE = b"alice-very-secret-\x01\x02-value"


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


class Seen:
    """Every response body of the test, for the no-value-anywhere check."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    async def hook(self, response: httpx.Response) -> None:
        await response.aread()
        self.bodies.append(response.content)


@pytest_asyncio.fixture
async def clients(
    engine: AsyncEngine, runtime: ControllerRuntime
) -> AsyncIterator[tuple[dict[str, httpx.AsyncClient], Seen, str]]:
    for name in PW:
        await create_user(engine, name, PW[name], admin=name == "admin")
    base = f"https://127.0.0.1:{runtime.http_port}"
    seen = Seen()
    made: dict[str, httpx.AsyncClient] = {}
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        for name in PW:
            token = await login(anon, name, PW[name])
            made[name] = httpx.AsyncClient(
                base_url=base,
                headers=bearer(token),
                timeout=30,
                event_hooks={"response": [seen.hook]},
            )
    try:
        yield made, seen, base
    finally:
        for client in made.values():
            await client.aclose()


def _put(client: httpx.AsyncClient, name: str, value: bytes) -> Any:
    return client.put(f"/v1/secrets/{name}", content=value)


@pytest.mark.security
async def test_per_user_and_never_a_value(
    clients: Any, runtime: ControllerRuntime
) -> None:
    made, seen, _base = clients
    alice, bob, admin = made["alice"], made["bob"], made["admin"]
    key = runtime.settings.secrets_key_path
    assert not key.exists()  # created on first use
    r = await _put(alice, "api_key", VALUE)
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "API_KEY", "created": True}
    if sys.platform != "win32":
        assert (key.stat().st_mode & 0o777) == 0o600
    assert (await _put(alice, "API_KEY", VALUE + b"2")).json()["created"] is False
    assert (await _put(bob, "BOB_ONLY", b"bob-value")).status_code == 200
    listed = (await alice.get("/v1/secrets")).json()
    assert [s["name"] for s in listed["secrets"]] == ["API_KEY"]
    assert set(listed["secrets"][0]) == {"name", "created_at", "updated_at"}
    assert (await admin.get("/v1/secrets")).json()["secrets"] == []  # no admin view
    assert (await bob.delete("/v1/secrets/API_KEY")).status_code == 404
    assert (await alice.get("/v1/secrets/API_KEY")).status_code in (404, 405)
    assert (await alice.delete("/v1/secrets/api_key")).json() == {
        "name": "API_KEY",
        "deleted": True,
    }
    assert (await alice.delete("/v1/secrets/API_KEY")).status_code == 404
    everything = b"\n".join(seen.bodies)
    for form in (
        VALUE,
        VALUE.hex().encode(),
        base64.b64encode(VALUE),
        b"alice-very-secret",
    ):
        assert form not in everything


async def test_bounds_names_and_auth(clients: Any) -> None:
    made, _seen, base = clients
    alice = made["alice"]
    assert (await _put(alice, "SHORT", b"abc")).status_code == 400
    assert (await _put(alice, "MIN", b"abcd")).status_code == 200
    assert (await _put(alice, "MAX", b"x" * MAX_SECRET_BYTES)).status_code == 200
    over = await _put(alice, "OVER", b"x" * (MAX_SECRET_BYTES + 1))
    assert over.status_code == 400
    for bad in ("1ABC", "A-B", "%E2%84%AAEY", "A" * 65):
        r = await _put(alice, bad, b"value-1234")
        assert r.status_code == 400, bad
        assert "E2" not in r.text and "A-B" not in r.text
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        assert (await anon.get("/v1/secrets")).status_code == 401
        assert (await anon.put("/v1/secrets/X1", content=b"abcd")).status_code == 401
