"""V0.8 G1 deliverable 1 (D170): the dashboard's browser session, end to end -
real PostgreSQL, a real HTTPS controller, real cookies.

THREAT_MODEL rows: DB-S1 (cookie theft), DB-T1 (CSRF), DB-S2 (session
fixation), DB-S3 (login CSRF / cross-origin), DB-E1 (WebSocket hijacking),
DB-S4 (idle / absolute expiry, revoke, logout).

Cookies are sent by hand (a ``Cookie`` header) so every test controls exactly
what the "browser" holds.
"""

from __future__ import annotations

import asyncio
import datetime as dt
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
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from meshforge.common.api_token import new_browser_session_token
from meshforge.controller.auth.browser import CSRF_HEADER, csrf_value
from meshforge.controller.auth.users import UserStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.auth import ApiToken
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
    pytest.mark.timeout(180),
]

ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={
            **USERS_ONLY,
            "login_free_failures": 2,
            "login_backoff_base_s": 30.0,
            "login_rate_per_minute": 600.0,
            "login_rate_burst": 100,
            "stream_recheck_interval_s": 0.2,
        },
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def users(engine: AsyncEngine, runtime: ControllerRuntime) -> dict[str, str]:
    return {
        "alice": await create_user(engine, "alice", ALICE_PW),
        "bob": await create_user(engine, "bob", BOB_PW),
    }


def _origin(runtime: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{runtime.http_port}"


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=_origin(runtime), timeout=30) as client:
        yield client


def _set_cookies(response: httpx.Response) -> dict[str, Any]:
    """Every ``Set-Cookie`` of ``response``, parsed (name -> morsel)."""
    jar: dict[str, Any] = {}
    for raw in response.headers.get_list("set-cookie"):
        parsed: SimpleCookie = SimpleCookie()
        parsed.load(raw)
        jar.update(parsed.items())
    return jar


async def _open(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    name: str = "alice",
    password: str = ALICE_PW,
    **headers: str,
) -> httpx.Response:
    return await anon.post(
        "/v1/session",
        json={"name": name, "password": password},
        headers={"origin": _origin(runtime), **headers},
    )


async def _session(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, name: str = "alice"
) -> tuple[str, str]:
    """``(session token, csrf value)`` from a real login."""
    response = await _open(anon, runtime, name, ALICE_PW if name == "alice" else BOB_PW)
    assert response.status_code == 200, response.text
    jar = _set_cookies(response)
    return jar["mf_session"].value, jar["mf_csrf"].value


def _cookie(session: str, csrf: str | None = None) -> dict[str, str]:
    value = f"mf_session={session}"
    if csrf is not None:
        value += f"; mf_csrf={csrf}"
    return {"cookie": value}


# ------------------------------------------------------------------ login


async def test_login_sets_httponly_secure_strict_cookies_and_no_token_in_the_body(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    response = await _open(anon, runtime)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"] == {"id": users["alice"], "name": "alice", "role": "user"}
    assert body["idle_timeout_s"] == 12 * 3600
    assert response.headers["cache-control"] == "no-store"
    jar = _set_cookies(response)
    session, csrf = jar["mf_session"], jar["mf_csrf"]
    assert session.value.startswith("mfbs_")
    assert session.value not in response.text  # the token is only in the cookie
    assert session["httponly"] and session["secure"]
    assert session["samesite"].lower() == "strict" and session["path"] == "/v1"
    assert int(session["max-age"]) == 7 * 86400
    assert not csrf["httponly"] and csrf["secure"]
    assert csrf["samesite"].lower() == "strict" and csrf["path"] == "/"
    assert csrf.value == csrf_value(session.value)
    # The session works for reads, and says who it is.
    me = await anon.get("/v1/session", headers=_cookie(session.value))
    assert me.status_code == 200
    assert me.json()["session"]["session_id"] == body["session_id"]
    nodes = await anon.get("/v1/nodes", headers=_cookie(session.value))
    assert nodes.status_code == 200


async def test_wrong_password_unknown_user_and_garbage_are_refused(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    bad = await _open(anon, runtime, "alice", "wrong-password")
    assert bad.status_code == 401 and "set-cookie" not in bad.headers
    unknown = await _open(anon, runtime, "nobody", "whatever-pass")
    assert unknown.status_code == 401
    assert bad.json()["error"]["message"] == unknown.json()["error"]["message"]
    garbage = await anon.post(
        "/v1/session",
        content=b"{not json",
        headers={"content-type": "application/json", "origin": _origin(runtime)},
    )
    assert garbage.status_code == 400 and "wrong" not in garbage.text


async def test_login_csrf_form_post_and_cross_origin_are_refused(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    form = await anon.post(
        "/v1/session",
        data={"name": "alice", "password": ALICE_PW},
        headers={"origin": _origin(runtime)},
    )
    assert form.status_code == 400 and "set-cookie" not in form.headers
    foreign = await _open(anon, runtime, origin="https://evil.example")
    assert foreign.status_code == 403
    assert foreign.json()["error"]["reason"] == "cross_origin"
    fetch_site = await _open(anon, runtime, **{"sec-fetch-site": "cross-site"})
    assert fetch_site.status_code == 403
    port = await _open(anon, runtime, origin="https://127.0.0.1:1")  # same site!
    assert port.status_code == 403


async def test_session_fixation_a_planted_cookie_is_never_adopted(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    planted = new_browser_session_token().get_secret_value()
    response = await _open(anon, runtime, cookie=f"mf_session={planted}")
    assert response.status_code == 200
    issued = _set_cookies(response)["mf_session"].value
    assert issued != planted
    # V0.8 G3 (D191 b): signed out is 200 {session: null}, never the planted one.
    refused = await anon.get("/v1/session", headers=_cookie(planted))
    assert refused.status_code == 200 and refused.json() == {"session": None}
    # Two logins never share a token.
    again, _ = await _session(anon, runtime)
    assert again != issued


async def test_the_login_backoff_is_shared_with_the_api_token_login(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    for _ in range(2):  # the free failures, through the dashboard door
        assert (await _open(anon, runtime, "bob", "wrong")).status_code == 401
    assert (await _open(anon, runtime, "bob", "wrong")).status_code == 401
    blocked = await anon.post(
        "/v1/auth/login", json={"name": "bob", "password": BOB_PW}
    )
    assert blocked.status_code == 429 and "retry-after" in blocked.headers
    blocked2 = await _open(anon, runtime, "bob", BOB_PW)
    assert blocked2.status_code == 429


# ------------------------------------------------------------------ CSRF


async def test_a_cookie_mutation_needs_the_csrf_header_bound_to_the_session(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    session, csrf = await _session(anon, runtime)
    api_token = await login(anon, "alice", ALICE_PW)
    whoami = await anon.get("/v1/auth/whoami", headers=bearer(api_token))
    token_id = whoami.json()["token_id"]
    path = f"/v1/auth/tokens/{token_id}"
    origin = {"origin": _origin(runtime)}

    no_header = await anon.delete(path, headers={**_cookie(session, csrf), **origin})
    assert no_header.status_code == 403
    assert no_header.json()["error"]["reason"] == "csrf"
    wrong = await anon.delete(
        path, headers={**_cookie(session, csrf), CSRF_HEADER: "x" * 43, **origin}
    )
    assert wrong.status_code == 403
    # Cookie tossing: header == cookie, but not this session's value.
    tossed = "A" * 43
    toss = await anon.delete(
        path, headers={**_cookie(session, tossed), CSRF_HEADER: tossed, **origin}
    )
    assert toss.status_code == 403
    no_cookie = await anon.delete(
        path, headers={**_cookie(session), CSRF_HEADER: csrf, **origin}
    )
    assert no_cookie.status_code == 403
    foreign = await anon.delete(
        path,
        headers={
            **_cookie(session, csrf),
            CSRF_HEADER: csrf,
            "origin": "https://evil.example",
        },
    )
    assert foreign.status_code == 403
    # Nothing above changed anything.
    assert (await anon.get("/v1/auth/whoami", headers=bearer(api_token))).is_success
    ok = await anon.delete(
        path, headers={**_cookie(session, csrf), CSRF_HEADER: csrf, **origin}
    )
    assert ok.status_code == 200, ok.text
    assert (
        await anon.get("/v1/auth/whoami", headers=bearer(api_token))
    ).status_code == 401


async def test_credentials_never_cross_between_header_and_cookie(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    session, _ = await _session(anon, runtime)
    api_token = await login(anon, "alice", ALICE_PW)
    # A stolen session cookie used as a Bearer token: unknown.
    as_bearer = await anon.get("/v1/nodes", headers=bearer(session))
    assert as_bearer.status_code == 401
    # An API token put into the cookie: unknown.
    in_cookie = await anon.get("/v1/nodes", headers=_cookie(api_token))
    assert in_cookie.status_code == 401
    # A bad Authorization header is judged alone - no fallback to the cookie.
    both = await anon.get(
        "/v1/nodes", headers={**_cookie(session), "authorization": "Bearer nope"}
    )
    assert both.status_code == 401


# ------------------------------------------------------- lifetime and revoke


async def _age(engine: AsyncEngine, session_id: str, **values: dt.datetime) -> None:
    async with engine.begin() as db:
        await db.execute(
            update(ApiToken).where(ApiToken.id == session_id).values(**values)
        )


async def test_idle_and_absolute_expiry(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    now = dt.datetime.now(dt.UTC)
    session, _ = await _session(anon, runtime)
    me = await anon.get("/v1/session", headers=_cookie(session))
    sid = me.json()["session"]["session_id"]
    # Unused for 13 h (> 12 h idle): ended, although far from its 7 d limit.
    old = now - dt.timedelta(hours=13)
    await _age(engine, sid, created_at=old, last_used_at=old)
    ended = await anon.get("/v1/session", headers=_cookie(session))
    assert ended.status_code == 200 and ended.json() == {"session": None}
    assert (await anon.get("/v1/nodes", headers=_cookie(session))).status_code == 401
    store = UserStore(engine)
    assert await store.live_token_ids([sid]) == set()  # D157 re-check agrees
    listed = await store.get_token(sid)
    assert listed is not None and listed.state == "expired"

    session2, _ = await _session(anon, runtime)
    sid2 = (await anon.get("/v1/session", headers=_cookie(session2))).json()["session"][
        "session_id"
    ]
    # Busy (used a minute ago) but past its absolute end.
    await _age(
        engine,
        sid2,
        created_at=now - dt.timedelta(days=8),
        last_used_at=now - dt.timedelta(minutes=1),
        expires_at=now - dt.timedelta(days=1),
    )
    assert (await anon.get("/v1/nodes", headers=_cookie(session2))).status_code == 401

    # Used 11 h ago: still live (the idle clock is last use, not creation).
    session3, _ = await _session(anon, runtime)
    sid3 = (await anon.get("/v1/session", headers=_cookie(session3))).json()["session"][
        "session_id"
    ]
    await _age(
        engine,
        sid3,
        created_at=now - dt.timedelta(days=2),
        last_used_at=now - dt.timedelta(hours=11),
    )
    assert (await anon.get("/v1/nodes", headers=_cookie(session3))).status_code == 200


async def test_sessions_are_listed_and_revoked_with_the_api_tokens(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    session, _ = await _session(anon, runtime)
    api_token = await login(anon, "alice", ALICE_PW)
    listed = await anon.get("/v1/auth/tokens", headers=bearer(api_token))
    kinds = {t["kind"] for t in listed.json()["tokens"]}
    assert kinds == {"api", "browser_session"}
    (row,) = [t for t in listed.json()["tokens"] if t["kind"] == "browser_session"]
    assert row["state"] == "active" and row["idle_expires_at"] is not None
    revoked = await anon.delete(
        f"/v1/auth/tokens/{row['id']}", headers=bearer(api_token)
    )
    assert revoked.status_code == 200
    assert (await anon.get("/v1/nodes", headers=_cookie(session))).status_code == 401


async def test_logout_revokes_server_side_and_clears_both_cookies(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    session, csrf = await _session(anon, runtime)
    origin = {"origin": _origin(runtime)}
    refused = await anon.delete(
        "/v1/session", headers={**_cookie(session, csrf), **origin}
    )
    assert refused.status_code == 403  # logout is a mutation: CSRF applies
    out = await anon.delete(
        "/v1/session", headers={**_cookie(session, csrf), CSRF_HEADER: csrf, **origin}
    )
    assert out.status_code == 200 and out.json() == {"logged_out": True}
    jar = _set_cookies(out)
    assert jar["mf_session"]["max-age"] == "0" and jar["mf_csrf"]["max-age"] == "0"
    assert (await anon.get("/v1/nodes", headers=_cookie(session))).status_code == 401
    async with engine.connect() as db:
        revoked = await db.scalar(
            text(
                "SELECT count(*) FROM api_token WHERE kind = 'browser_session' "
                "AND revoked_at IS NOT NULL"
            )
        )
    assert revoked == 1
    # A dead session still gets its cookies cleared.
    again = await anon.delete("/v1/session", headers=_cookie(session))
    assert again.status_code == 200 and again.json() == {"logged_out": False}
    assert _set_cookies(again)["mf_session"]["max-age"] == "0"


# ------------------------------------------------------------- WebSocket


async def test_websocket_with_the_cookie_needs_our_own_origin_and_is_rechecked(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    session, csrf = await _session(anon, runtime)
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/job_x/logs/ws"
    for origin in (None, "https://evil.example", "https://127.0.0.1:1"):
        headers = {"cookie": f"mf_session={session}"}
        if origin is not None:
            headers["origin"] = origin
        with pytest.raises(InvalidStatus) as refused:
            async with connect(url, additional_headers=headers):
                pass
        assert refused.value.response.status_code == 403
    # Our own origin: accepted (the bad job id then closes it 4400, after accept).
    async with connect(
        url,
        additional_headers={
            "cookie": f"mf_session={session}",
            "origin": _origin(runtime),
        },
    ) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), 10)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4400
    assert csrf  # (the CSRF header is not needed for a read stream)
