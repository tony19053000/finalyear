"""V0.8 G2 tester r2: the T-3 workaround (_send_first) swallows ONLY the
handshake-abort refusal, never a real error, and never after the first frame."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from uvicorn.protocols.utils import ClientDisconnected  # noqa: E402

from meshforge.controller.api import event_routes as er  # noqa: E402


class _WS:
    def __init__(self, exc: BaseException | None) -> None:
        self.exc = exc
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        if self.exc is not None:
            raise self.exc
        self.sent.append(text)


async def test_sends_the_frame_normally() -> None:
    ws = _WS(None)
    await er._send_first(ws, {"type": "hello"})  # type: ignore[arg-type]
    assert ws.sent == ['{"type":"hello"}']


#: uvicorn's refusal text (uvicorn/protocols/websockets/*_impl.py, accept state).
_REFUSAL = (
    "Expected ASGI message 'websocket.accept', 'websocket.close', or "
    "'websocket.http.response.start' but got 'websocket.send'."
)


async def test_runtimeerror_on_first_frame_is_a_quiet_disconnect() -> None:
    with pytest.raises(ClientDisconnected):
        await er._send_first(_WS(RuntimeError(_REFUSAL)), {"a": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text", ["Expected ASGI message", "boom", "Cannot call send once a close message"]
)
async def test_other_runtimeerrors_on_first_frame_propagate(text: str) -> None:
    """V0.8 G3 item 0 (D191 a): only the accept refusal is a disconnect."""
    with pytest.raises(RuntimeError, match=text):
        await er._send_first(_WS(RuntimeError(text)), {"a": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "exc", [TypeError("x"), ValueError("x"), OSError("x"), KeyError("x")]
)
async def test_other_errors_on_first_frame_still_propagate(exc: Exception) -> None:
    with pytest.raises(type(exc)):
        await er._send_first(_WS(exc), {"a": 1})  # type: ignore[arg-type]


async def test_unserialisable_payload_still_raises() -> None:
    with pytest.raises(TypeError):
        await er._send_first(_WS(None), {"a": object()})  # type: ignore[arg-type]


async def test_slow_consumer_timeout_on_first_frame_is_still_slow_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    class Slow(_WS):
        async def send_text(self, text: str) -> None:
            await asyncio.sleep(5)

    monkeypatch.setattr(er, "_SEND_TIMEOUT_S", 0.05)
    with pytest.raises(er._EndedError):
        await er._send_first(Slow(None), {"a": 1})  # type: ignore[arg-type]


async def test_later_frames_never_swallow_runtimeerror() -> None:
    """Only the first frame has the workaround: a RuntimeError from a later
    send is a real bug and must surface."""
    with pytest.raises(RuntimeError):
        await er._send(_WS(RuntimeError("boom")), {"a": 1})  # type: ignore[arg-type]


async def test_workaround_is_used_only_for_the_hello_frame() -> None:
    import inspect

    src = inspect.getsource(er)
    assert src.count("_send_first(") == 2  # definition + the one hello call
