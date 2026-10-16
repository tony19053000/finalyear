"""V0.8 G3 item 0 (b) (D189, D191 b): ``GET /v1/session`` is the dashboard's
"am I signed in" probe - signed out is ``200 {session: null}``, not a 401 the
browser logs as a console error on every first visit.

Real PostgreSQL, a real HTTPS controller, cookies sent by hand. The probe
must never widen access: an ``Authorization`` header is still judged alone
(a bad one is 401), an API token is still 400 ``not_a_session``, and a dead
session's answer carries nothing but ``null`` (and clears its cookies).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http.cookies import SimpleCookie
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.auth.browser import CSRF_HEADER
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
    pytest.mark.timeout(180),
]

ALICE_PW = "alice-password-1"
SIGNED_OUT = {"session": None}


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


@pytest_asyncio.fixture
async def alice(engine: AsyncEngine, runtime: ControllerRuntime) -> str:
    return await create_user(engine, "alice", ALICE_PW)


def _origin(runtime: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{runtime.http_port}"


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=_origin(runtime), timeout=30) as client:
        yield client


def _set_cookies(response: httpx.Response) -> dict[str, Any]:
    jar: dict[str, Any] = {}
    for raw in response.headers.get_list("set-cookie"):
        parsed: SimpleCookie = SimpleCookie()
        parsed.load(raw)
        jar.update(parsed.items())
    return jar


async def _session(
    anon: httpx.AsyncClient, runtime: ControllerRuntime
) -> tuple[str, str]:
    response = await anon.post(
        "/v1/session",
        json={"name": "alice", "password": ALICE_PW},
        headers={"origin": _origin(runtime)},
    )
    assert response.status_code == 200, response.text
    jar = _set_cookies(response)
    return jar["mf_session"].value, jar["mf_csrf"].value


def _cookie(session: str) -> dict[str, str]:
    return {"cookie": f"mf_session={session}"}


async def test_no_cookie_is_200_null_with_no_cookies_set(
    anon: httpx.AsyncClient, alice: str
) -> None:
    response = await anon.get("/v1/session")
    assert response.status_code == 200
    assert response.json() == SIGNED_OUT
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get_list("set-cookie") == []


async def test_a_live_session_answers_who_and_until_when(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, alice: str
) -> None:
    session, _ = await _session(anon, runtime)
    response = await anon.get("/v1/session", headers=_cookie(session))
    assert response.status_code == 200
    body = response.json()["session"]
    assert body["user"] == {"id": alice, "name": "alice", "role": "user"}
    assert body["session_id"].startswith("tok_")
    assert body["idle_timeout_s"] == 12 * 3600
    assert session not in response.text  # the token stays in the cookie
    assert response.headers.get_list("set-cookie") == []


async def test_a_logged_out_session_is_null_and_its_cookies_are_cleared(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, alice: str
) -> None:
    session, csrf = await _session(anon, runtime)
    out = await anon.delete(
        "/v1/session",
        headers={
            "cookie": f"mf_session={session}; mf_csrf={csrf}",
            CSRF_HEADER: csrf,
            "origin": _origin(runtime),
        },
    )
    assert out.status_code == 200
    response = await anon.get("/v1/session", headers=_cookie(session))
    assert response.status_code == 200
    assert response.json() == SIGNED_OUT  # nothing about the dead session
    jar = _set_cookies(response)
    assert jar["mf_session"]["max-age"] == "0" and jar["mf_csrf"]["max-age"] == "0"
    # The probe is not a way in: every other route still refuses the cookie.
    assert (await anon.get("/v1/nodes", headers=_cookie(session))).status_code == 401


async def test_a_cookie_that_is_not_a_session_token_is_null(
    anon: httpx.AsyncClient, alice: str
) -> None:
    for value in ("garbage", "mfbs_" + "x" * 3, "mf_" + "a" * 40):
        response = await anon.get("/v1/session", headers=_cookie(value))
        assert response.status_code == 200 and response.json() == SIGNED_OUT


async def test_an_authorization_header_is_judged_alone(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, alice: str
) -> None:
    session, _ = await _session(anon, runtime)
    bad = await anon.get("/v1/session", headers={"authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == "Bearer"
    # A bad header with a good cookie: still 401, no fallback to the cookie.
    both = await anon.get(
        "/v1/session", headers={**_cookie(session), "authorization": "Bearer nope"}
    )
    assert both.status_code == 401
    # An API token is a user, but not a dashboard session.
    token = await login(anon, "alice", ALICE_PW)
    api = await anon.get("/v1/session", headers=bearer(token))
    assert api.status_code == 400
    assert api.json()["error"]["reason"] == "not_a_session"
