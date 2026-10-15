"""V0.8 G2 item 0 tester r1 (D184): attack the exact-port Host guard - every
spelling of the port, IPv6 + port, missing port, the listener's own port."""

from __future__ import annotations

import string

import pytest
from starlette.types import Message, Receive, Scope, Send

from meshforge.controller.web.host_guard import LoopbackHostGuard, allowed_host

pytestmark = [pytest.mark.unit, pytest.mark.security]

PORT = 47111


@pytest.mark.parametrize("name", ["localhost", "LocalHost", "127.0.0.1", "[::1]"])
def test_own_port_passes_for_every_loopback_name(name: str) -> None:
    assert allowed_host(f"{name}:{PORT}", PORT)


@pytest.mark.parametrize(
    "host",
    [
        "[::1]:",  # empty port
        "localhost:",
        "127.0.0.1:",
        f"::1:{PORT}",  # unbracketed IPv6
        f"[::1]{PORT}",  # missing colon
        f"[::1]::{PORT}",
        f"[::1]:{PORT}:",
        f"[::1]:{PORT}:{PORT}",
        f"[::1]:0{PORT}",
        f"[::1]:+{PORT}",
        f"[::1]: {PORT}",
        f"[::1]:{PORT} ",
        f" localhost:{PORT}",
        f"localhost:{PORT}\t",
        f"localhost:{PORT}\n",
        f"localhost:{PORT}\r",
        f"localhost:{PORT}\x00",
        f"localhost:{PORT}/",
        f"localhost:{PORT}?x",
        f"localhost:{PORT}#x",
        f"localhost:{PORT}_",
        f"localhost:{PORT}\u200b",  # zero-width space
        f"localhost\u200b:{PORT}",
        f"localhost:{PORT}\u0660",  # arabic-indic zero appended
        "localhost:\u0664\u0667\u0661\u0661\u0661",  # arabic-indic digits
        "localhost:\u1810\u1811",  # mongolian digits
        "localhost:\u00b2",  # superscript two (isdigit True, not decimal)
        f"[::1]:{PORT}\u2028",
        f"localhost:{PORT},localhost:{PORT}",
        f"localhost:{PORT};x",
        f"localhost@localhost:{PORT}",
        f"localhost:{PORT}@evil.example",
        f"evil.example:{PORT}@localhost",
        f"localhost:{PORT}.",
        f"localhost.:{PORT}",
        f"[::1].:{PORT}",
        f"[0:0:0:0:0:0:0:1]:{PORT}",
        f"[::0001]:{PORT}",
        f"[::ffff:127.0.0.1]:{PORT}",
        f"0.0.0.0:{PORT}",
        f"localhost:{PORT + 1}",
        f"localhost:{PORT - 1}",
        "localhost:0",
        "localhost:00",
        f"localhost:{PORT:x}",
        f"localhost:0x{PORT:x}",
        f"localhost:{PORT}.0",
        f"localhost:{PORT}e0",
        f"localhost:{PORT + 65536}",  # wraps if masked to 16 bits
        f"localhost:{PORT + 2**32}",
        f"localhost:{PORT}{'0' * 70}",
    ],
)
def test_foreign_or_mangled_port_spellings_are_refused(host: str) -> None:
    assert not allowed_host(host, PORT)


def test_no_single_ascii_character_perturbation_of_the_port_passes() -> None:
    base = f"localhost:{PORT}"
    for ch in string.printable + "\x00\x7f":
        for i in range(len("localhost:"), len(base) + 1):
            assert not allowed_host(base[:i] + ch + base[i:], PORT), repr(ch)
        assert not allowed_host(base + ch, PORT) or ch == ""


def test_a_port_that_is_a_prefix_or_suffix_of_the_real_one_is_refused() -> None:
    for text in (str(PORT)[:-1], str(PORT)[1:], "4711", "7111"):
        assert not allowed_host(f"localhost:{text}", PORT)


@pytest.mark.parametrize("port", [1, 7, 80, 4711, 65535])
def test_exact_port_for_various_listeners_and_no_cross_acceptance(port: int) -> None:
    assert allowed_host(f"[::1]:{port}", port)
    assert not allowed_host(f"[::1]:{port}", port + 1 if port < 65535 else 1)
    assert not allowed_host(f"[::1]:0{port}", port)


def test_port_zero_listener_only_matches_a_literal_zero() -> None:
    # An unbound/ephemeral port 0 is never a real Host port; nothing else matches.
    assert not allowed_host("localhost:00", 0)
    assert not allowed_host(f"localhost:{PORT}", 0)


def test_no_port_form_is_independent_of_the_listener_port() -> None:
    for port in (0, 1, PORT, 65535):
        assert allowed_host("[::1]", port) and allowed_host("localhost", port)


async def _drive(host: bytes | None, kind: str) -> list[Message]:
    sent: list[Message] = []
    reached: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(True)

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    headers = [] if host is None else [(b"host", host)]
    await LoopbackHostGuard(app, port=PORT)(
        {"type": kind, "headers": headers}, receive, send
    )
    return [{"type": "REACHED"}] if reached else sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        f"[::1]:0{PORT}".encode(),
        f"[::1]:{PORT} ".encode(),
        f"localhost:{PORT}\x00".encode(),
        b"[::1]:",
        b"::1",
        None,
    ],
)
async def test_end_to_end_refusals_http_and_websocket(host: bytes | None) -> None:
    http = await _drive(host, "http")
    assert http[0]["type"] == "http.response.start" and http[0]["status"] == 400
    ws = await _drive(host, "websocket")
    assert ws == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host", [f"[::1]:{PORT}".encode(), f"LOCALHOST:{PORT}".encode()]
)
async def test_end_to_end_own_port_reaches_the_app(host: bytes) -> None:
    assert await _drive(host, "http") == [{"type": "REACHED"}]
    assert await _drive(host, "websocket") == [{"type": "REACHED"}]
