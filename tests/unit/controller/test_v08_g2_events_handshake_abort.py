"""V0.8 G2 fix round 1, tester T-3: a client that aborts the ``/v1/events/ws``
handshake must not make the controller log "Exception in ASGI application".

Cause: uvicorn's sans-I/O WebSocket protocol (the one ``uvicorn[standard]``
selects) silently drops ``websocket.accept`` when the client's TCP connection
is already closing, then refuses the route's first ``websocket.send`` with a
``RuntimeError``. The race is made deterministic here: the REAL uvicorn
protocol object gets a fake transport that reports ``is_closing()`` from the
moment the upgrade request was parsed, before the route runs.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("uvicorn", reason="needs the controller extra")

from fastapi import FastAPI
from uvicorn.config import Config
from uvicorn.protocols.websockets.websockets_sansio_impl import (
    WebSocketsSansIOProtocol,
)
from uvicorn.server import ServerState

from meshforge.controller.api import event_routes
from meshforge.controller.events.bus import EventBus

pytestmark = pytest.mark.unit

UPGRADE = (
    b"GET /v1/events/ws HTTP/1.1\r\n"
    b"Host: 127.0.0.1:47110\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Version: 13\r\n"
    b"Authorization: Bearer test\r\n"
    b"\r\n"
)


class ClosingTransport(asyncio.Transport):
    """A transport whose peer is gone (``is_closing``) once ``closing`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.closing = False
        self.written = bytearray()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {"peername": ("127.0.0.1", 50000), "sockname": ("127.0.0.1", 47110)}.get(
            name, default
        )

    def is_closing(self) -> bool:
        return self.closing

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self.written += bytes(data)

    def close(self) -> None:
        self.closing = True

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


def _app(bus: EventBus) -> FastAPI:
    app = FastAPI()
    app.include_router(event_routes.event_router)
    setattr(app.state, event_routes.EVENTS_STATE_KEY, bus)
    return app


async def _run_aborted_handshake(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> tuple[EventBus, ClosingTransport]:
    user = SimpleNamespace(user_id="u1", is_admin=True, name="admin")

    async def authenticate(_ws: Any) -> Any:
        return user

    # Authentication has its own tests; here it only has to say "yes".
    monkeypatch.setattr(event_routes, "authenticate", authenticate)
    monkeypatch.setattr(
        event_routes, "get_auth", lambda _ws: SimpleNamespace(live_streams=None)
    )
    bus = EventBus()
    config = Config(app=_app(bus), lifespan="off", ws="websockets-sansio")
    config.load()
    protocol = WebSocketsSansIOProtocol(config, ServerState(), {})
    transport = ClosingTransport()
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    protocol.connection_made(transport)
    protocol.data_received(UPGRADE)  # the route task is created, not yet run
    transport.closing = True  # ...and the client leaves before it runs
    tasks = list(protocol.tasks)
    assert tasks, "uvicorn did not start the ASGI task"
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
    return bus, transport


@pytest.mark.asyncio
async def test_client_gone_during_handshake_is_not_an_asgi_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bus, transport = await _run_aborted_handshake(monkeypatch, caplog)
    messages = [r.getMessage() for r in caplog.records if r.name == "uvicorn.error"]
    assert not any("Exception in ASGI application" in m for m in messages), messages
    assert not any("without completing handshake" in m for m in messages), messages
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert b"500" not in bytes(transport.written)
    assert len(bus) == 0, "the follower must be unsubscribed"


@pytest.mark.asyncio
async def test_the_uvicorn_behaviour_this_guards_against_still_exists(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If uvicorn ever stops dropping ``accept`` on a closing transport, the
    ``_send_first`` workaround becomes dead code: this test says so."""
    calls: list[str] = []
    original = event_routes._send_first

    async def spy(ws: Any, payload: dict[str, Any]) -> None:
        try:
            await event_routes._send(ws, payload)
        except RuntimeError as exc:
            calls.append(str(exc))
        await original(ws, payload)

    monkeypatch.setattr(event_routes, "_send_first", spy)
    await _run_aborted_handshake(monkeypatch, caplog)
    assert calls and "Expected ASGI message 'websocket.accept'" in calls[0], calls
