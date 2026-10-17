"""V0.9 G0 tester round 1: ``GET /v1/clock`` against the real HTTPS controller
and real PostgreSQL - the dashboard cookie, API tokens, logout and revoke."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]
PASSWORD = "dave-password-1"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 60.0},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def user(pg_settings: DatabaseSettings, runtime: ControllerRuntime) -> str:
    engine = create_engine(pg_settings)
    try:
        await create_user(engine, "dave", PASSWORD)
    finally:
        await engine.dispose()
    return "dave"


def _client(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    )


async def _ok(c: httpx.AsyncClient, **kw: object) -> httpx.Response:
    return await c.get("/v1/clock", **kw)  # type: ignore[arg-type]


async def test_browser_cookie_session_reads_the_clock_without_csrf_then_logout_kills_it(
    runtime: ControllerRuntime, user: str
) -> None:
    async with _client(runtime) as c:
        opened = await c.post("/v1/session", json={"name": user, "password": PASSWORD})
        assert opened.status_code == 200, opened.text
        got = await _ok(c)  # cookie only, GET, no CSRF header
        assert got.status_code == 200 and "server_time" in got.json()
        csrf = c.cookies.get("mf_csrf")
        assert csrf
        out = await c.delete("/v1/session", headers={"x-meshforge-csrf": csrf})
        assert out.status_code == 200, out.text
        # the old cookie value, replayed by hand, is dead server side
        replay = httpx.Cookies()
        replay.set(
            "mf_session", opened.cookies.get("mf_session") or "", domain="127.0.0.1"
        )
    async with _client(runtime) as c2:
        c2.cookies = replay
        assert (await _ok(c2)).status_code == 401


async def test_clock_cookie_rules_a_session_token_as_bearer_and_api_token_in_cookie_are_refused(
    runtime: ControllerRuntime, user: str
) -> None:
    async with _client(runtime) as anon:
        api_token = await login(anon, user, PASSWORD)
    async with _client(runtime) as c:
        await c.post("/v1/session", json={"name": user, "password": PASSWORD})
        session_token = c.cookies.get("mf_session")
        assert session_token
    async with _client(runtime) as c:
        assert (await _ok(c, headers=bearer(session_token))).status_code == 401
    async with _client(runtime) as c:
        c.cookies.set("mf_session", api_token, domain="127.0.0.1")
        assert (await _ok(c)).status_code == 401
    async with _client(runtime) as c:
        assert (await _ok(c, headers=bearer(api_token))).status_code == 200


async def test_clock_a_revoked_api_token_is_refused_on_the_next_request(
    runtime: ControllerRuntime, user: str
) -> None:
    async with _client(runtime) as anon:
        keep = await login(anon, user, PASSWORD)
        doomed = await login(anon, user, PASSWORD)
        assert (await _ok(anon, headers=bearer(doomed))).status_code == 200
        listed = await anon.get("/v1/auth/whoami", headers=bearer(doomed))
        token_id = listed.json().get("token_id")
        if token_id is None:
            tokens = (await anon.get("/v1/auth/tokens", headers=bearer(doomed))).json()
            token_id = next(t["id"] for t in tokens["tokens"] if t.get("current"))
        gone = await anon.delete(f"/v1/auth/tokens/{token_id}", headers=bearer(keep))
        assert gone.status_code == 200, gone.text
        assert (await _ok(anon, headers=bearer(doomed))).status_code == 401
        assert (await _ok(anon, headers=bearer(keep))).status_code == 200


async def test_clock_head_and_unauthenticated_do_not_leak_a_time(
    runtime: ControllerRuntime, user: str
) -> None:
    async with _client(runtime) as c:
        r = await c.get("/v1/clock")
        assert r.status_code == 401 and "server_time" not in r.text
        h = await c.head("/v1/clock")
        assert h.status_code in {401, 405}


async def test_clock_answers_monotonically_close_to_local_time_under_load(
    runtime: ControllerRuntime, user: str
) -> None:
    import asyncio
    import time

    async with _client(runtime) as anon:
        tok = await login(anon, user, PASSWORD)
        before = time.time()
        rs = await asyncio.gather(*[_ok(anon, headers=bearer(tok)) for _ in range(40)])
        after = time.time()
    times = [r.json()["server_time"] for r in rs]
    assert all(r.status_code == 200 for r in rs)
    assert all(before - 0.05 <= t <= after + 0.05 for t in times)
