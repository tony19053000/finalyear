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
        assert me.status_code == 200 and me.json()["session"] is not None
    # The LAN listener is still TLS-only.
    async with httpx.AsyncClient(timeout=5) as client:
        with pytest.raises(httpx.HTTPError):
            await client.get(f"http://127.0.0.1:{runtime.http_port}/healthz")


async def _raw_upgrade(port: int, host: str) -> bytes:
    """A WebSocket upgrade to the event stream with ``Host: host``; the status line."""
    import asyncio

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            (
                "GET /v1/events/ws HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        return await asyncio.wait_for(reader.readline(), 10)
    finally:
        writer.close()


async def test_loopback_plain_refuses_a_rebound_host_name(
    runtime: ControllerRuntime, engine: AsyncEngine
) -> None:
    """D182 O-1 (DNS rebinding): a page of evil.example rebound to 127.0.0.1
    still sends ``Host: evil.example``; the plain listener refuses it (HTTP and
    WebSocket) and keeps answering the loopback names."""
    await create_user(engine, "alice", ALICE_PW)
    port = runtime.plain_port
    assert port is not None
    plain = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=plain, timeout=10) as client:
        for host in (f"evil.example:{port}", "evil.example", f"localhost:{port + 1}"):
            refused = await client.post(
                "/v1/session",
                json={"name": "alice", "password": ALICE_PW},
                headers={"host": host, "origin": f"http://{host}"},
            )
            assert refused.status_code == 400, (host, refused.text)
            assert refused.json()["error"]["code"] == "invalid_input"
            assert "mf_session" not in refused.cookies
            assert refused.headers.get("x-frame-options") == "DENY"
            health = await client.get("/healthz", headers={"host": host})
            assert health.status_code == 400
        for host in (f"localhost:{port}", "127.0.0.1", f"[::1]:{port}"):
            assert (
                await client.get("/healthz", headers={"host": host})
            ).status_code == 200
    assert b" 101 " not in await _raw_upgrade(port, f"evil.example:{port}")
    # The same upgrade with a loopback Host reaches the app (which then
    # refuses it for having no credential - after accepting, or with 403).
    status = await _raw_upgrade(port, f"127.0.0.1:{port}")
    assert status.startswith(b"HTTP/1.1"), status
