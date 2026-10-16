"""V0.8 G5 item 0b (D195): pin the uvicorn text ``_send_first`` relies on.

``event_routes._send_first`` turns a ``RuntimeError`` into a quiet client
disconnect only when its text contains ``_ACCEPT_REFUSAL``
(``"websocket.accept"``, D191 a). That text is uvicorn's, not ours: if an
upgrade rewords it, the workaround silently stops matching and a client that
aborts the handshake is logged as "Exception in ASGI application" again. These
tests make each installed uvicorn WebSocket protocol refuse a
``websocket.send`` that comes before ``websocket.accept`` (the state a dropped
accept leaves behind) and fail loudly, naming the uvicorn version, when the
text no longer matches.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("uvicorn", reason="needs the controller extra")

import uvicorn
from uvicorn.config import Config
from uvicorn.server import ServerState

from meshforge.controller.api import event_routes

pytestmark = pytest.mark.unit

SANSIO = (
    "uvicorn.protocols.websockets.websockets_sansio_impl",
    "WebSocketsSansIOProtocol",
)
LEGACY = ("uvicorn.protocols.websockets.websockets_impl", "WebSocketProtocol")
WSPROTO = ("uvicorn.protocols.websockets.wsproto_impl", "WSProtocol")
PINNED = {SANSIO, LEGACY, WSPROTO}

UPGRADE = (
    b"GET /v1/events/ws HTTP/1.1\r\n"
    b"Host: 127.0.0.1:47110\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Version: 13\r\n"
    b"\r\n"
)


class _Transport(asyncio.Transport):
    """An open transport that keeps what is written."""

    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()
        self.closed = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {"peername": ("127.0.0.1", 50000), "sockname": ("127.0.0.1", 47110)}.get(
            name, default
        )

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self.written += bytes(data)

    def close(self) -> None:
        self.closed = True

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


async def _noop_app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
    return None


def _config(app: Any = _noop_app, ws: str = "auto") -> Config:
    config = Config(app=app, lifespan="off", ws=ws)
    config.load()
    return config


async def _refusal_via_real_upgrade(module: str, name: str) -> str:
    """Run a real upgrade through the protocol; the app sends before accepting."""
    refusals: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()  # websocket.connect
        try:
            await send({"type": "websocket.send", "text": "{}"})
        except RuntimeError as exc:
            refusals.append(str(exc))
        await send({"type": "websocket.close", "code": 1000})

    cls = getattr(importlib.import_module(module), name)
    protocol = cls(_config(app), ServerState(), {})
    transport = _Transport()
    protocol.connection_made(transport)
    protocol.data_received(UPGRADE)
    tasks = list(protocol.tasks)
    assert tasks, f"uvicorn's {name} did not start the ASGI task"
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
    assert refusals, f"uvicorn's {name} accepted a send before websocket.accept"
    return refusals[0]


def _check(text: str, name: str) -> None:
    assert event_routes._ACCEPT_REFUSAL in text, (
        f"uvicorn {uvicorn.__version__} ({name}) now refuses a send before accept "
        f"with {text!r}; event_routes._send_first matches "
        f"{event_routes._ACCEPT_REFUSAL!r} and would let a handshake abort log "
        "'Exception in ASGI application' again. Update _ACCEPT_REFUSAL (and the "
        "D191 a tests) to the new wording."
    )
    # The exact sentence today: a reworded one must be reviewed, not absorbed.
    assert text.startswith("Expected ASGI message 'websocket.accept'"), text


@pytest.mark.asyncio
async def test_sansio_protocol_refuses_with_the_pinned_text() -> None:
    """The protocol ``uvicorn[standard]`` runs the controller with."""
    pytest.importorskip("websockets", reason="uvicorn's sans-I/O protocol needs it")
    _check(await _refusal_via_real_upgrade(*SANSIO), SANSIO[1])


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore")  # websockets.legacy + uvicorn deprecation
async def test_legacy_websockets_protocol_refuses_with_the_pinned_text() -> None:
    pytest.importorskip("websockets", reason="uvicorn's websockets protocol needs it")
    cls = getattr(importlib.import_module(LEGACY[0]), LEGACY[1])
    protocol = cls(_config(), ServerState(), {})
    with pytest.raises(RuntimeError) as refused:
        await protocol.asgi_send({"type": "websocket.send", "text": "{}"})
    _check(str(refused.value), LEGACY[1])


@pytest.mark.asyncio
async def test_wsproto_protocol_refuses_with_the_pinned_text() -> None:
    pytest.importorskip("wsproto", reason="uvicorn's wsproto protocol needs wsproto")
    _check(await _refusal_via_real_upgrade(*WSPROTO), WSPROTO[1])


def test_the_protocol_uvicorn_picks_by_default_is_pinned_here() -> None:
    """``ws="auto"`` (what the controller runs) must be a protocol checked above."""
    chosen = _config().ws_protocol_class
    assert chosen is not None
    assert (chosen.__module__, chosen.__name__) in PINNED, (
        f"uvicorn {uvicorn.__version__} picks {chosen.__module__}.{chosen.__name__}, "
        "which this file does not pin; add it here"
    )
