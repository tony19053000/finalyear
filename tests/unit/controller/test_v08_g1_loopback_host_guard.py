"""V0.8 G1 fix round 1 (D182 O-1): the plain-HTTP loopback listener answers
only ``Host`` localhost / 127.0.0.1 / [::1] (optionally with its own port) -
DNS rebinding cannot turn a hostile page into a same-origin client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from meshforge.controller.web.host_guard import LoopbackHostGuard, allowed_host
from meshforge.controller.web.spa import SECURITY_HEADERS

pytestmark = pytest.mark.unit

PORT = 47111


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "127.0.0.1",
        "[::1]",
        f"localhost:{PORT}",
        f"127.0.0.1:{PORT}",
        f"[::1]:{PORT}",
    ],
)
def test_loopback_names_with_no_port_or_our_port_pass(host: str) -> None:
    assert allowed_host(host, PORT)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "evil.example",
        f"evil.example:{PORT}",
        "localhost.",
        f"localhost.:{PORT}",
        "localhost.evil.example",
        "127.0.0.1.nip.io",
        "127.0.0.2",
        "127.1",
        "2130706433",
        "0x7f000001",
        "[::ffff:127.0.0.1]",
        "::1",
        "[::1",
        "localhost:1",
        f"localhost:{PORT + 1}",
        "localhost:",
        f"localhost:0{PORT}0",
        "localhost:+47111",
        "localhost:٤٧١١١",  # non-ASCII digits
        f"user@localhost:{PORT}",
        f"localhost:{PORT}:{PORT}",
        "localhost " + str(PORT),
        "x" * 70,
    ],
)
def test_every_other_host_is_refused(host: str) -> None:
    assert not allowed_host(host, PORT)


@pytest.mark.parametrize(
    "host",
    [
        f"localhost:0{PORT}",
        f"127.0.0.1:00{PORT}",
        f"[::1]:0{PORT}",
        f"LOCALHOST:000{PORT}",
        f"localhost:{PORT}.0",
        f"localhost:{PORT}e0",
        f"localhost:-{PORT}",
        "localhost:\uff14\uff17\uff11\uff11\uff11",  # fullwidth digits
    ],
)
def test_port_must_be_the_exact_decimal_digits(host: str) -> None:
    """D184 (4): ``int()`` would accept ``047111``; only the canonical spelling
    of the listener's port passes."""
    assert not allowed_host(host, PORT)


@pytest.mark.parametrize("port", [80, 443, 8080, 65535])
def test_exact_port_passes_for_other_listener_ports(port: int) -> None:
    assert allowed_host(f"localhost:{port}", port)
    assert not allowed_host(f"localhost:0{port}", port)


@pytest.mark.asyncio
async def test_a_zero_padded_port_is_refused_end_to_end() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", f"localhost:0{PORT}".encode("ascii"))],
    }
    await LoopbackHostGuard(_app, port=PORT)(scope, receive, send)
    assert sent[0]["status"] == 400
    assert b"APP" not in b"".join(m.get("body", b"") for m in sent)


async def _app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"APP"})


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{PORT}"
    )


@pytest.mark.asyncio
async def test_a_loopback_host_reaches_the_app() -> None:
    async with _client(LoopbackHostGuard(_app, port=PORT)) as client:
        for host in ("localhost", f"127.0.0.1:{PORT}", "[::1]"):
            resp = await client.get("/v1/nodes", headers={"host": host})
            assert resp.status_code == 200 and resp.text == "APP", host


@pytest.mark.asyncio
@pytest.mark.security
async def test_a_rebound_name_gets_the_error_envelope_with_security_headers() -> None:
    async with _client(LoopbackHostGuard(_app, port=PORT)) as client:
        for host in (f"evil.example:{PORT}", "localhost:1", "evil.example"):
            resp = await client.get("/healthz", headers={"host": host})
            assert resp.status_code == 400, host
            assert resp.json()["error"]["code"] == "invalid_input"
            assert "APP" not in resp.text and "evil" not in resp.text  # no echo
            for name, value in SECURITY_HEADERS:
                assert resp.headers.get(name) == value, name


async def _call(guard: LoopbackHostGuard, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent


@pytest.mark.security
@pytest.mark.parametrize(
    "headers",
    [
        [],  # no Host (HTTP/1.0)
        [(b"host", b"localhost"), (b"host", b"evil.example")],  # repeated
        [(b"host", "lócalhost".encode())],  # not ASCII
        [(b"host", f"evil.example:{PORT}".encode())],
    ],
)
@pytest.mark.asyncio
async def test_missing_repeated_or_foreign_host_fails_closed_for_http_and_ws(
    headers: list[tuple[bytes, bytes]],
) -> None:
    guard = LoopbackHostGuard(_app, port=PORT)
    http = await _call(guard, {"type": "http", "headers": headers})
    assert http[0]["type"] == "http.response.start" and http[0]["status"] == 400
    ws = await _call(guard, {"type": "websocket", "headers": headers})
    assert ws == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_a_websocket_with_a_loopback_host_is_passed_through() -> None:
    guard = LoopbackHostGuard(_app, port=PORT)
    ws = await _call(guard, {"type": "websocket", "headers": [(b"host", b"[::1]")]})
    assert ws == [{"type": "websocket.accept"}]


@pytest.mark.asyncio
async def test_lifespan_scopes_pass_untouched() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    await LoopbackHostGuard(app, port=PORT)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]
