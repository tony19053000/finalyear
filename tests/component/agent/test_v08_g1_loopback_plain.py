"""V0.8 G1 deliverable 5 (brief Q1): the optional plain-HTTP loopback listener
serves the same API (same auth, same dashboard session) on 127.0.0.1 only,
while the LAN port stays HTTPS. Real controller, real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import create_user
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
    pytest.mark.timeout(120),
]

ALICE_PW = "alice-password-1"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY},
        http={"loopback_plain": "127.0.0.1:0"},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


async def test_loopback_plain_serves_the_same_api_and_auth(
    runtime: ControllerRuntime, engine: AsyncEngine
) -> None:
    await create_user(engine, "alice", ALICE_PW)
    assert runtime.plain_port is not None and runtime._plain_sock is not None
    assert runtime._plain_sock.getsockname()[0] == "127.0.0.1"
    plain = f"http://127.0.0.1:{runtime.plain_port}"
    async with httpx.AsyncClient(base_url=plain, timeout=10) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/v1/nodes")).status_code == 401  # same auth
        login = await client.post(
            "/v1/session",
            json={"name": "alice", "password": ALICE_PW},
            headers={"origin": plain},
        )
        assert login.status_code == 200, login.text
        cookie = login.cookies.get("mf_session")
        assert cookie
        me = await client.get("/v1/session", headers={"cookie": f"mf_session={cookie}"})
        assert me.status_code == 200
    # The LAN listener is still TLS-only.
    async with httpx.AsyncClient(timeout=5) as client:
        with pytest.raises(httpx.HTTPError):
            await client.get(f"http://127.0.0.1:{runtime.http_port}/healthz")
