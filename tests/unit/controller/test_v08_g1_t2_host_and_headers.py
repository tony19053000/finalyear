"""V0.8 G1 tester r2: Host guard bypass attempts (D182 O-1) and security
headers on every error path (D182 T-1), plus paging boundary instants (T-4)."""

# ruff: noqa: E501, S104, DTZ001, N813, PIE810, RUF001, SIM105

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import threading

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("uvicorn", reason="needs the controller extra")

import httpx
import uvicorn
from fastapi import FastAPI

from meshforge.controller.web.host_guard import LoopbackHostGuard, allowed_host

pytestmark = [pytest.mark.unit, pytest.mark.security]

PORT = 47111


@pytest.mark.parametrize(
    "host",
    [
        "LOCALHOST",
        "LocalHost:47111",
        "127.0.0.1:47111",
        "[::1]:47111",
        "[::1]",
        "localhost:47111",
    ],
)
def test_allowed_variants(host) -> None:
    assert allowed_host(host, PORT)


@pytest.mark.parametrize(
    "host",
    [
        "",
        " localhost",
        "localhost ",
        "localhost.",
        "localhost.:47111",
        "localhost:",
        "localhost:47111 ",
        "localhost:+47111",
        "localhost:4_7111",
        "localhost:47111:47111",
        "localhost:99999",
        "localhost:0",
        "::1",
        "[::1",
        "::1]",
        "[::1].",
        "[0:0:0:0:0:0:0:1]",
        "[::0001]",
        "[::ffff:127.0.0.1]",
        "[::1%25lo]",
        "[::1%lo]:47111",
        "127.1",
        "127.0.0.01",
        "0x7f.0.0.1",
        "2130706433",
        "0.0.0.0",
        "localhost\t",
        "localhost\x00",
        "localhost\u200b",
        "ⅼocalhost",
        "localhost@evil",
        "evil@localhost",
        "localhost#x",
        "localhost/",
        "localhost,evil",
        "evil,localhost",
        "localhost:47111,evil",
        "xlocalhost",
        "localhost.evil.example",
        "a" * 100,
        "LOCALHOSTK",
        "localhost:٤٧١١١",
    ],
)
def test_refused_variants(host) -> None:
    assert not allowed_host(host, PORT)


async def _drive(scope_headers, scope_type="http", path="/healthz"):
    sent: list[dict] = []
    reached: list[bool] = []

    async def app(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    scope = {
        "type": scope_type,
        "path": path,
        "headers": scope_headers,
        "query_string": b"",
    }
    await LoopbackHostGuard(app, port=PORT)(scope, receive, send)
    return sent, bool(reached)


@pytest.mark.asyncio
async def test_header_name_case_and_duplicate_variants() -> None:
    _, ok = await _drive([(b"host", b"localhost")])
    assert ok
    # header names are lowercase in ASGI; a mixed-case name must still count
    _, ok = await _drive([(b"Host", b"localhost")])
    assert ok
    for hs in (
        [(b"host", b"localhost"), (b"host", b"localhost")],
        [(b"host", b"localhost"), (b"host", b"evil.example")],
        [(b"host", b"evil.example"), (b"host", b"localhost")],
        [(b"host", b"localhost"), (b"Host", b"evil.example")],
        [],
        [(b"x-forwarded-host", b"localhost")],
        [(b"host", b"\xff")],
    ):
        sent, ok = await _drive(hs)
        assert not ok, hs
        assert sent[0]["status"] == 400
        names = {k for k, _ in sent[0]["headers"]}
        assert (
            b"x-content-type-options" in names and b"content-security-policy" in names
        )
        assert b"evil" not in sent[1]["body"]
    # websocket: close before accept, never reaches the app
    for hs in (
        [(b"host", b"evil.example")],
        [],
        [(b"host", b"localhost"), (b"host", b"localhost")],
    ):
        sent, ok = await _drive(hs, "websocket", "/v1/events")
        assert not ok
        assert sent == [{"type": "websocket.close", "code": 1008}]
    # lifespan passes through untouched
    sent, ok = await _drive([], "lifespan")
    assert ok


async def _raw(port: int, payload: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(payload)
    await w.drain()
    data = b""
    try:
        data = await asyncio.wait_for(r.read(65536), 3)
    except TimeoutError:
        pass
    w.close()
    return data


@pytest.fixture
def server():
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    cfg = uvicorn.Config(
        LoopbackHostGuard(app, port=port), log_config=None, lifespan="off"
    )
    srv = uvicorn.Server(cfg)
    t = threading.Thread(
        target=lambda: asyncio.run(srv.serve(sockets=[s])), daemon=True
    )
    t.start()
    for _ in range(100):
        if srv.started:
            break
        import time

        time.sleep(0.05)
    yield port
    srv.should_exit = True
    t.join(5)


@pytest.mark.asyncio
async def test_real_server_raw_request_forms(server) -> None:
    p = server
    ok = await _raw(
        p,
        b"GET /healthz HTTP/1.1\r\nHost: localhost:%d\r\nConnection: close\r\n\r\n" % p,
    )
    assert ok.startswith(b"HTTP/1.1 200")
    # absolute-form target naming an allowed host but Host header rebound -> refused
    r = await _raw(
        p,
        b"GET http://localhost/healthz HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n",
    )
    assert r.startswith(b"HTTP/1.1 400"), r[:80]
    # absolute-form naming evil with a loopback Host header: the guard keys on Host only;
    # the app routes by path, so this is not a bypass of anything (document: still served)
    r = await _raw(
        p,
        b"GET http://evil.example/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    assert r.startswith(b"HTTP/1.1 200") or r.startswith(b"HTTP/1.1 400")
    # duplicate Host on the wire
    r = await _raw(
        p,
        b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nHost: evil.example\r\nConnection: close\r\n\r\n",
    )
    assert r.startswith(b"HTTP/1.1 400"), r[:80]
    # obs-fold / space-before-colon / tab tricks
    for hdr in (
        b"Host : evil.example\r\n",
        b"Host:\tevil.example\r\n",
        b" Host: evil.example\r\n",
    ):
        r = await _raw(
            p, b"GET /healthz HTTP/1.1\r\n" + hdr + b"Connection: close\r\n\r\n"
        )
        assert not r.startswith(b"HTTP/1.1 200"), (hdr, r[:80])
    # HTTP/1.0 without Host: fail closed
    r = await _raw(p, b"GET /healthz HTTP/1.0\r\n\r\n")
    assert not r.startswith(b"HTTP/1.0 200") and not r.startswith(b"HTTP/1.1 200"), r[
        :80
    ]
    # uppercase
    r = await _raw(
        p,
        b"GET /healthz HTTP/1.1\r\nHOST: LOCALHOST:%d\r\nConnection: close\r\n\r\n" % p,
    )
    assert r.startswith(b"HTTP/1.1 200")


@pytest.mark.asyncio
async def test_real_server_websocket_upgrade_with_bad_host_is_not_101(server) -> None:
    p = server
    for host in (b"evil.example", b"localhost.", b"127.0.0.2"):
        r = await _raw(
            p,
            b"GET /v1/events HTTP/1.1\r\nHost: "
            + host
            + b"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n",
        )
        assert b" 101 " not in r.split(b"\r\n", 1)[0], host


# ---- headers on every error path (T-1) -------------------------------------


def _build(web_dist=None):
    from types import SimpleNamespace as ns

    from mf_users import static_auth

    from meshforge.controller.http import create_http_app

    class _Manager:
        def __len__(self) -> int:
            return 0

    app = create_http_app(
        _Manager(),
        registry=ns(),
        resources=ns(),
        clock=lambda: 0.0,
        auth=static_auth(),
        web_dist=web_dist,
    )

    @app.get("/v1/_t2_boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/v1/_t2_int/{n}")
    async def typed(n: int) -> dict:
        return {"n": n}

    return app


_REQUIRED = (
    "x-content-type-options",
    "content-security-policy",
    "x-frame-options",
    "referrer-policy",
)


@pytest.mark.asyncio
async def test_security_headers_on_every_error_path(tmp_path) -> None:
    from mf_users import ADMIN_TOKEN, bearer

    (tmp_path / "index.html").write_text("<html></html>")
    for dist in (None, tmp_path):
        app = _build(dist)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost"
        ) as c:
            admin = bearer(ADMIN_TOKEN)
            cases = [
                ("GET", "/v1/_t2_boom", admin, 500),
                ("GET", "/v1/_t2_int/abc", admin, 422),
                ("GET", "/v1/nope-does-not-exist", admin, 404),
                ("POST", "/healthz", {}, 405),
                ("GET", "/v1/_t2_boom", {}, 401),
                ("GET", "/v1/_t2_boom", {"authorization": "Bearer \u00e9"}, None),
                ("GET", "/v1/jobs?limit=-1", admin, 422),
                (
                    "GET",
                    "/v1/jobs?created_after=0001-01-01T00:00:00%2B23:59",
                    admin,
                    None,
                ),
            ]
            for method, path, hdrs, status in cases:
                try:
                    resp = await c.request(method, path, headers=hdrs)
                except (UnicodeEncodeError, httpx.InvalidURL):
                    continue
                if status is not None and not (
                    dist is None and path == "/v1/nope-does-not-exist"
                ):
                    assert resp.status_code in (status, 404, 400, 401), (
                        path,
                        resp.status_code,
                    )
                for h in _REQUIRED:
                    assert h in resp.headers, (dist, method, path, resp.status_code, h)


@pytest.mark.asyncio
async def test_headers_survive_middleware_exception() -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    app = _build()

    class _Bad(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            raise RuntimeError("middleware boom")

    app.add_middleware(_Bad)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 500
    for h in _REQUIRED:
        assert h in resp.headers, h


# ---- paging boundary instants (T-4) -----------------------------------------


def test_utc_instant_boundaries() -> None:
    from meshforge.controller.api.errors import ApiError
    from meshforge.controller.api.paging import (
        decode_cursor,
        encode_cursor,
        utc_instant,
    )

    tz = lambda h, m=0: dt.timezone(dt.timedelta(hours=h, minutes=m))  # noqa: E731
    for bad in (
        dt.datetime(1, 1, 1, 0, 0, tzinfo=tz(23, 59)),
        dt.datetime(9999, 12, 31, 23, 59, 59, tzinfo=tz(-23, -59)),
        dt.datetime(1, 1, 1, 0, 0, 0, tzinfo=tz(1)),
    ):
        with pytest.raises(ApiError):
            utc_instant(bad, name="x")
    for good in (
        dt.datetime(1, 1, 1, tzinfo=dt.UTC),
        dt.datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC),
        dt.datetime(1, 1, 2, 0, 0, tzinfo=tz(23)),
        dt.datetime(2026, 1, 1, tzinfo=tz(5, 30)),
    ):
        assert utc_instant(good, name="x").tzinfo is not None
    with pytest.raises(ApiError):
        utc_instant(dt.datetime(2026, 1, 1), name="x")  # naive
    # round trip at the extreme valid instants
    for at in (
        dt.datetime(1, 1, 1, tzinfo=dt.UTC),
        dt.datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC),
    ):
        assert (
            decode_cursor(
                encode_cursor(at, "job_01J0000000000000000000000A"),
                validate_id=lambda _: None,
            )[0]
            == at
        )
