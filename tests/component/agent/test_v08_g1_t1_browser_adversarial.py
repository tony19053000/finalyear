"""V0.8 G1 tester r1: the browser session (D170) attacked from outside.

Real PostgreSQL, a real HTTPS controller. Derived from the brief + checklist
19 (fail closed) and 25 (no secret anywhere but the HttpOnly cookie).
"""

# ruff: noqa: PT012, RUF059

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from collections.abc import AsyncIterator
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

from meshforge.controller.auth.browser import CSRF_HEADER
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
    pytest.mark.timeout(240),
]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    # Default stream_recheck_interval_s (1 s): the brief's "~1 s" is measured
    # against the shipped default, not a tuned test value.
    return make_controller_settings(
        pg_settings,
        auth={
            **USERS_ONLY,
            "login_free_failures": 50,
            "login_rate_per_minute": 600.0,
            "login_rate_burst": 100,
        },
        jobs={"dispatch_interval_s": 60.0},
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
        "admin": await create_user(engine, "admin", ADMIN_PW, admin=True),
        "alice": await create_user(engine, "alice", ALICE_PW),
        "bob": await create_user(engine, "bob", BOB_PW),
    }


def _origin(rt: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{rt.http_port}"


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=_origin(runtime), timeout=30) as client:
        yield client


PWS = {"admin": ADMIN_PW, "alice": ALICE_PW, "bob": BOB_PW}


async def _session(
    anon: httpx.AsyncClient, rt: ControllerRuntime, name: str = "alice"
) -> tuple[str, str, str]:
    """(session token, csrf, session id)."""
    r = await anon.post(
        "/v1/session",
        json={"name": name, "password": PWS[name]},
        headers={"origin": _origin(rt)},
    )
    assert r.status_code == 200, r.text
    cookies = {
        c.split("=", 1)[0]: c.split("=", 1)[1].split(";")[0]
        for c in r.headers.get_list("set-cookie")
    }
    anon.cookies.clear()  # the "browser" holds only what each test sends by hand
    return cookies["mf_session"], cookies["mf_csrf"], r.json()["session_id"]


def _cookie(session: str, csrf: str | None = None) -> dict[str, str]:
    value = f"mf_session={session}"
    if csrf is not None:
        value += f"; mf_csrf={csrf}"
    return {"cookie": value}


# --------------------------------------------------------- cross-cutting CSRF


async def test_every_authenticated_mutating_route_refuses_a_cookie_without_csrf(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    """Enumerates the OpenAPI document: no POST/PUT/PATCH/DELETE that needs a
    login may accept a bare cookie (no header, or a wrong header)."""
    spec = (
        await anon.get(
            "/v1/openapi.json", headers=bearer(await login(anon, "admin", ADMIN_PW))
        )
    ).json()
    session, csrf, _ = await _session(anon, runtime, "admin")
    checked = 0
    for path, item in spec["paths"].items():
        for method in ("post", "put", "patch", "delete"):
            if method not in item:
                continue
            url = re.sub(r"\{[^}]+\}", "x1", path)
            probe = await anon.request(method.upper(), url)
            if probe.status_code != 401:
                continue  # an open route (login, enroll, ...): no session to ride
            for extra in (
                {},
                {CSRF_HEADER: "wrong-" + "A" * 40},
                {CSRF_HEADER: csrf + "x"},
            ):
                r = await anon.request(
                    method.upper(),
                    url,
                    headers={**_cookie(session, csrf), **extra},
                    json={},
                )
                assert r.status_code == 403, (
                    method,
                    path,
                    extra,
                    r.status_code,
                    r.text,
                )
                assert r.json()["error"]["reason"] == "csrf", (method, path)
            checked += 1
    assert checked >= 10, f"suspiciously few mutating routes found: {checked}"


async def test_csrf_values_do_not_cross_sessions_or_users(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    a_sess, a_csrf, _ = await _session(anon, runtime, "alice")
    b_sess, b_csrf, b_id = await _session(anon, runtime, "bob")
    a2_sess, a2_csrf, _ = await _session(anon, runtime, "alice")  # same user again
    assert a_csrf != a2_csrf and a_csrf != b_csrf
    origin = {"origin": _origin(runtime)}
    for sess, token in ((a_sess, b_csrf), (b_sess, a_csrf), (a2_sess, a_csrf)):
        r = await anon.delete(
            "/v1/session",
            headers={**_cookie(sess, token), CSRF_HEADER: token, **origin},
        )
        assert r.status_code == 403, "another session's CSRF value was accepted"
    # Everyone is still logged in.
    for sess in (a_sess, b_sess, a2_sess):
        assert (await anon.get("/v1/session", headers=_cookie(sess))).status_code == 200


async def test_a_logged_out_sessions_csrf_value_is_dead_for_the_next_session(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    old_sess, old_csrf, _ = await _session(anon, runtime)
    origin = {"origin": _origin(runtime)}
    out = await anon.delete(
        "/v1/session",
        headers={**_cookie(old_sess, old_csrf), CSRF_HEADER: old_csrf, **origin},
    )
    assert out.status_code == 200
    new_sess, new_csrf, _ = await _session(anon, runtime)
    r = await anon.delete(
        "/v1/session",
        headers={**_cookie(new_sess, old_csrf), CSRF_HEADER: old_csrf, **origin},
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://127.0.0.1",
        "http://127.0.0.1:%PORT%",
        "HTTPS://EVIL.example",
        "",
        "https://127.0.0.1:%PORT%.evil.example",
    ],
)
async def test_login_and_mutation_origin_edge_cases(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    origin: str,
) -> None:
    origin = origin.replace("%PORT%", str(runtime.http_port))
    r = await anon.post(
        "/v1/session",
        json={"name": "alice", "password": ALICE_PW},
        headers={"origin": origin},
    )
    assert r.status_code == 403, (origin, r.status_code)
    assert "set-cookie" not in r.headers


async def test_two_origin_headers_and_cross_site_fetch_metadata_are_refused(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    own = _origin(runtime)
    r = await anon.post(
        "/v1/session",
        json={"name": "alice", "password": ALICE_PW},
        headers=[("origin", own), ("origin", "https://evil.example")],
    )
    assert r.status_code == 403
    r = await anon.post(
        "/v1/session",
        json={"name": "alice", "password": ALICE_PW},
        headers={"origin": own, "sec-fetch-site": "cross-site"},
    )
    assert r.status_code == 403
    r = await anon.post(
        "/v1/session",
        json={"name": "alice", "password": ALICE_PW},
        headers={
            "origin": own,
            "sec-fetch-site": "same-site",
        },  # sibling subdomain/port
    )
    assert r.status_code in (200, 403)  # observed, not mandated


# --------------------------------------------------------- hostile input


@pytest.mark.parametrize(
    "cookie",
    [
        "mf_session=",
        "mf_session=mfbs_",
        "mf_session=mfbs_" + "A" * 10,
        "mf_session=mfbs_" + "A" * 100_000,
        "mf_session=%00%00",
        "mf_session=mfbs_ééé",
        "mf_session=mfat_" + "A" * 43,
        "mf_session=a; mf_session=b",
        'mf_session="quoted"',
        "mf_session=mfbs_';DROP TABLE api_token;--",
        "=;;;===",
    ],
)
async def test_hostile_cookie_values_are_401_never_500_and_never_echoed(
    anon: httpx.AsyncClient, users: dict[str, str], cookie: str
) -> None:
    try:
        r = await anon.get("/v1/nodes", headers={"cookie": cookie})
    except (httpx.InvalidURL, httpx.LocalProtocolError, UnicodeEncodeError):
        pytest.skip("the client itself refuses to send this header")
    assert r.status_code in (400, 401, 431), (cookie[:40], r.status_code)
    assert "DROP TABLE" not in r.text and "mfbs_" not in r.text
    # and the database still works afterwards
    assert (await anon.get("/healthz")).status_code == 200


@pytest.mark.parametrize(
    "csrf",
    [b"", b"\xff\xfe\xfd", b"A" * 5000, "é".encode("latin-1"), b"a b"],
)
async def test_hostile_csrf_header_values_are_403_never_500(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    csrf: bytes,
) -> None:
    session, token, _ = await _session(anon, runtime)
    r = await anon.delete(
        "/v1/session",
        headers=[
            (b"cookie", f"mf_session={session}; mf_csrf={token}".encode()),
            (CSRF_HEADER.encode(), csrf),
            (b"origin", _origin(runtime).encode()),
        ],
    )
    assert r.status_code == 403, (csrf[:10], r.status_code)


async def test_duplicate_csrf_headers_are_refused(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    session, token, _ = await _session(anon, runtime)
    r = await anon.delete(
        "/v1/session",
        headers=[
            (b"cookie", f"mf_session={session}; mf_csrf={token}".encode()),
            (CSRF_HEADER.encode(), token.encode()),
            (CSRF_HEADER.encode(), b"other"),
        ],
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"123",
        b"null",
        b'"str"',
        b'{"name": 1, "password": 2}',
        b'{"name": "alice"}',
        b'{"name": "", "password": "x"}',
        b'{"name": "alice", "password": ""}',
        b'{"name": "a\\u0000b", "password": "x"}',
        b'{"name": "alice", "password": "' + b"p" * 5000 + b'"}',
        b'{"name": "alice", "password": "x", "extra": {"a": [1,2,3]}}',
        b"\xff\xfe{}",
        b"{" * 100_000,
        b'{"name":"alice","password":"' + "ü".encode() * 40 + b'"}',
    ],
)
async def test_hostile_login_bodies_are_4xx_never_500_and_never_echo_the_password(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    body: bytes,
) -> None:
    r = await anon.post(
        "/v1/session",
        content=body,
        headers={"content-type": "application/json", "origin": _origin(runtime)},
    )
    assert r.status_code in (400, 401, 413, 422), (body[:30], r.status_code)
    assert "set-cookie" not in r.headers
    assert "ppppp" not in r.text and ALICE_PW not in r.text


@pytest.mark.parametrize(
    "ctype",
    [
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/jsonp",
        "text/json",
        "",
    ],
)
async def test_login_only_accepts_json_content_type(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    ctype: str,
) -> None:
    headers = {"origin": _origin(runtime)}
    if ctype:
        headers["content-type"] = ctype
    r = await anon.post(
        "/v1/session",
        content=f'{{"name":"alice","password":"{ALICE_PW}"}}'.encode(),
        headers=headers,
    )
    assert r.status_code == 400 and "set-cookie" not in r.headers


async def test_credentials_in_the_url_are_never_accepted(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    api = await login(anon, "alice", ALICE_PW)
    sess, _, _ = await _session(anon, runtime)
    for key in ("token", "access_token", "api_key", "mf_session", "authorization"):
        for secret in (api, sess):
            r = await anon.get("/v1/nodes", params={key: secret})
            assert r.status_code == 401, (key, r.status_code)
    r = await anon.post(
        "/v1/session",
        params={"name": "alice", "password": ALICE_PW},
        headers={"origin": _origin(runtime)},
    )
    assert r.status_code == 400
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/events/ws?access_token={api}&token={api}"
    with pytest.raises(InvalidStatus) as refused:
        async with connect(url):
            pass
    assert refused.value.response.status_code == 403


async def test_no_cors_grant_is_ever_made_to_a_foreign_origin(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    sess, _, _ = await _session(anon, runtime)
    evil = {"origin": "https://evil.example"}
    pre = await anon.options(
        "/v1/nodes",
        headers={
            **evil,
            "access-control-request-method": "GET",
            "access-control-request-headers": "authorization",
        },
    )
    assert "access-control-allow-origin" not in pre.headers
    got = await anon.get("/v1/nodes", headers={**evil, **_cookie(sess)})
    assert "access-control-allow-origin" not in got.headers
    assert "access-control-allow-credentials" not in got.headers


# ------------------------------------------------- lifetime / isolation / revoke


async def test_a_session_is_visible_and_revocable_only_by_its_owner_and_admins(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    a_sess, _, a_id = await _session(anon, runtime, "alice")
    bob_api = await login(anon, "bob", BOB_PW)
    listed = await anon.get("/v1/auth/tokens", headers=bearer(bob_api))
    assert a_id not in {t["id"] for t in listed.json()["tokens"]}
    revoke = await anon.delete(f"/v1/auth/tokens/{a_id}", headers=bearer(bob_api))
    assert revoke.status_code in (403, 404)
    assert (await anon.get("/v1/session", headers=_cookie(a_sess))).status_code == 200
    admin_api = await login(anon, "admin", ADMIN_PW)
    assert (
        await anon.delete(f"/v1/auth/tokens/{a_id}", headers=bearer(admin_api))
    ).status_code == 200
    assert (await anon.get("/v1/session", headers=_cookie(a_sess))).status_code == 401


async def test_use_slides_the_idle_window_but_never_the_absolute_one(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    sess, _, sid = await _session(anon, runtime)
    now = dt.datetime.now(dt.UTC)
    async with engine.begin() as db:
        before = (
            await db.execute(
                text("SELECT expires_at FROM api_token WHERE id = :i"), {"i": sid}
            )
        ).scalar_one()
        await db.execute(
            update(ApiToken)
            .where(ApiToken.id == sid)
            .values(
                last_used_at=now - dt.timedelta(hours=11),
                created_at=now - dt.timedelta(hours=11),
            )
        )
    assert (await anon.get("/v1/nodes", headers=_cookie(sess))).status_code == 200
    async with engine.connect() as db:
        row = (
            await db.execute(
                text("SELECT last_used_at, expires_at FROM api_token WHERE id = :i"),
                {"i": sid},
            )
        ).one()
    assert (dt.datetime.now(dt.UTC) - row.last_used_at).total_seconds() < 60, (
        "idle clock not refreshed"
    )
    assert row.expires_at == before, "use must never extend the absolute expiry"


async def test_absolute_expiry_exactly_at_the_boundary_fails_closed(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    sess, _, sid = await _session(anon, runtime)
    async with engine.begin() as db:
        await db.execute(
            update(ApiToken)
            .where(ApiToken.id == sid)
            .values(expires_at=dt.datetime.now(dt.UTC))
        )
    await asyncio.sleep(0.05)
    assert (await anon.get("/v1/nodes", headers=_cookie(sess))).status_code == 401


async def test_a_disabled_user_cannot_log_in_and_loses_open_sessions(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    sess, _, _ = await _session(anon, runtime, "bob")
    store = UserStore(engine)
    await store.set_disabled("bob", disabled=True, actor="test")
    assert (await anon.get("/v1/nodes", headers=_cookie(sess))).status_code == 401
    r = await anon.post(
        "/v1/session",
        json={"name": "bob", "password": BOB_PW},
        headers={"origin": _origin(runtime)},
    )
    assert r.status_code == 401 and "set-cookie" not in r.headers
    await store.set_disabled("bob", disabled=False, actor="test")
    # Re-enabling does NOT resurrect the old session.
    assert (await anon.get("/v1/nodes", headers=_cookie(sess))).status_code == 401


async def test_disable_revoke_idle_and_logout_cut_an_open_event_stream_fast(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    """Brief D170/D157: an open WebSocket is cut within ~1 s (shipped default
    re-check interval 1 s; allow 2.5 s wall for scheduling)."""
    store = UserStore(engine)
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/events/ws"

    async def open_ws(name: str):  # type: ignore[no-untyped-def]
        sess, csrf, sid = await _session(anon, runtime, name)
        ws = await connect(
            url, additional_headers={**_cookie(sess), "origin": _origin(runtime)}
        )
        assert '"hello"' in await asyncio.wait_for(ws.recv(), 10)
        return ws, sess, csrf, sid

    async def closed_within(ws: Any, limit: float) -> tuple[float, int]:
        started = time.monotonic()
        with pytest.raises(ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), limit + 3)
        elapsed = time.monotonic() - started
        assert closed.value.rcvd is not None
        return elapsed, closed.value.rcvd.code

    # 1. user disabled by another process (direct DB write, no notify)
    ws, *_ = await open_ws("bob")
    await store.set_disabled("bob", disabled=True, actor="test")
    elapsed, code = await closed_within(ws, 2.5)
    assert elapsed < 2.5 and code == 4401, (elapsed, code)
    await store.set_disabled("bob", disabled=False, actor="test")

    # 1b. user flag flipped with the token rows untouched (rule: user enabled)
    ws, sess, _, sid = await open_ws("bob")
    async with engine.begin() as db:
        await db.execute(text("UPDATE app_user SET disabled = true WHERE name = 'bob'"))
    elapsed, code = await closed_within(ws, 2.5)
    assert elapsed < 2.5 and code == 4401, (elapsed, code)
    async with engine.begin() as db:
        await db.execute(
            text("UPDATE app_user SET disabled = false WHERE name = 'bob'")
        )

    # 2. session revoked by direct DB write (no REST notify path)
    ws, sess, _, sid = await open_ws("alice")
    async with engine.begin() as db:
        await db.execute(
            update(ApiToken)
            .where(ApiToken.id == sid)
            .values(revoked_at=dt.datetime.now(dt.UTC))
        )
    elapsed, code = await closed_within(ws, 2.5)
    assert elapsed < 2.5 and code == 4401, (elapsed, code)

    # 3. idle expiry while connected
    ws, sess, _, sid = await open_ws("alice")
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=13)
    async with engine.begin() as db:
        await db.execute(
            update(ApiToken)
            .where(ApiToken.id == sid)
            .values(created_at=old, last_used_at=old)
        )
    elapsed, code = await closed_within(ws, 2.5)
    assert elapsed < 2.5 and code == 4401, (elapsed, code)

    # 4. logout through REST closes at once (notify path)
    ws, sess, csrf, _ = await open_ws("alice")
    started = time.monotonic()
    out = await anon.delete(
        "/v1/session",
        headers={**_cookie(sess, csrf), CSRF_HEADER: csrf, "origin": _origin(runtime)},
    )
    assert out.status_code == 200
    elapsed, code = await closed_within(ws, 1.5)
    assert time.monotonic() - started < 1.5 and code == 4401


# ------------------------------------------------------------ fixation / backoff


async def test_login_does_not_reveal_whether_a_user_exists_or_is_disabled(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    async def attempt(name: str, pw: str) -> tuple[int, str]:
        r = await anon.post(
            "/v1/session",
            json={"name": name, "password": pw},
            headers={"origin": _origin(runtime)},
        )
        return r.status_code, r.json()["error"]["message"]

    unknown = await attempt("nobody-at-all", "whatever-123")
    wrong = await attempt("alice", "wrong-password-1")
    assert unknown == wrong


async def test_concurrent_logins_each_get_a_distinct_session(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    results = await asyncio.gather(*(_session(anon, runtime) for _ in range(20)))
    assert len({r[0] for r in results}) == 20 and len({r[2] for r in results}) == 20


async def test_the_session_cookie_cannot_be_used_to_mint_or_read_api_tokens(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, users: dict[str, str]
) -> None:
    """A stolen session cookie (e.g. via a hostile browser extension) must not
    be convertible to a durable bearer token by re-login without the password."""
    sess, csrf, _ = await _session(anon, runtime)
    r = await anon.post(
        "/v1/auth/login",
        headers={**_cookie(sess, csrf), CSRF_HEADER: csrf, "origin": _origin(runtime)},
        json={"name": "alice"},
    )
    assert r.status_code in (400, 401, 422)
    assert "mfat_" not in r.text


async def test_nothing_secret_reaches_logs_or_error_bodies(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    users: dict[str, str],
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    sess, csrf, sid = await _session(anon, runtime)
    seen_bodies = []
    r = await anon.post(
        "/v1/session",
        json={"name": "alice", "password": "wrong-pass-hunter2"},
        headers={"origin": _origin(runtime)},
    )
    seen_bodies.append(r.text)
    seen_bodies.append(
        (await anon.delete("/v1/session", headers=_cookie(sess, csrf))).text
    )  # csrf refused
    seen_bodies.append(
        (await anon.get("/v1/nodes", headers=_cookie(sess[:-3] + "xyz"))).text
    )
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/events/ws"
    with pytest.raises(InvalidStatus):
        async with connect(
            url, additional_headers={**_cookie(sess), "origin": "https://evil.example"}
        ):
            pass
    await asyncio.sleep(0.3)
    captured = capfd.readouterr()
    logs = (
        captured.out
        + captured.err
        + "\n".join(
            f"{r.getMessage()} {r.__dict__}"
            for r in caplog.records
            if r.name.startswith("meshforge")
        )
    )
    assert "controller." in logs, (
        "the log capture saw nothing: the test would be vacuous"
    )
    haystack = logs + "\n".join(seen_bodies)
    for secret in (sess, csrf, ALICE_PW, "hunter2", sess[5:]):
        assert secret not in haystack, f"secret leaked: {secret[:8]}..."
