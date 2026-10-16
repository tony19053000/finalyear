"""``/v1/events/ws``: the dashboard's one live stream (V0.8 G1, D172).

Authenticated like every route: the dashboard's session cookie (with an
``Origin`` of the controller's own, D170) or ``Authorization: Bearer`` (the
CLI, scripts). Never a token in the URL. Query: ``since`` (the last ``seq`` the
client saw) and ``epoch`` (the bus epoch it saw it in).

First frame, then events::

    {"type": "hello", "epoch": "...", "seq": N, "resumed": true | false}
    {"type": "resync", "epoch": "...", "seq": N, "reason": "ring|epoch|future"}
    {"seq": 41, "type": "job.state", "at": 1760000000.123, "data": {...}}

``hello`` with ``resumed: true``: every event after ``since`` follows, in
order, none missing. ``resync``: the client must refetch what it shows (the
events from ``seq`` on still follow). A user receives node events and the
events of their own jobs; an admin everything, including ``alert``.

Closed ``4008 slow_consumer`` when the client falls behind (its queue filled
or a send blocked ``_SEND_TIMEOUT_S``), ``4029`` past the subscriber limit,
``4401 unauthorized`` / ``4503 auth_unavailable`` when the D157 re-check ends
its token or session. A refused handshake is a 403 (close before accept).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Coroutine
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import authenticate, get_auth
from meshforge.controller.api.errors import ApiError
from meshforge.controller.auth.live_streams import StreamAuth, StreamEndReason
from meshforge.controller.auth.principal import Principal
from meshforge.controller.events.bus import EventBus, Subscriber

__all__ = ["EVENTS_STATE_KEY", "event_router"]

log = get_logger(__name__)

EVENTS_STATE_KEY: Final = "meshforge_events"
CLOSE_SLOW_CONSUMER: Final = 4008
CLOSE_TOO_MANY: Final = 4029
CLOSE_UNAUTHORIZED: Final = 4401
CLOSE_AUTH_UNAVAILABLE: Final = 4503
_SEND_TIMEOUT_S: Final = 10.0
_CLOSE_WAIT_S: Final = 5.0
_MAX_EPOCH_CHARS: Final = 32

event_router = APIRouter(prefix="/v1/events", tags=["events"])


class _EndedError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, separators=(",", ":"))
    try:
        await asyncio.wait_for(ws.send_text(text), _SEND_TIMEOUT_S)
    except TimeoutError:
        raise _EndedError(CLOSE_SLOW_CONSUMER, "slow_consumer") from None


async def _send_first(ws: WebSocket, payload: dict[str, Any]) -> None:
    """The first frame after ``accept()``; a client that aborted the handshake
    is a disconnect, not an application error (V0.8 G2 tester T-3).

    uvicorn's sans-I/O WebSocket protocol drops ``websocket.accept`` without a
    word when the client's TCP connection is already closing, and then refuses
    the next ``websocket.send`` with ``RuntimeError("Expected ASGI message
    'websocket.accept' ...")`` - logged as "Exception in ASGI application".
    Straight after a successful ``accept()`` Starlette's own state is
    ``CONNECTED``, so a ``RuntimeError`` here can only be that refusal: end the
    connection as the server's own ``ClientDisconnected`` (which uvicorn ends
    quietly), never as a traceback.
    """
    try:
        await _send(ws, payload)
    except RuntimeError:
        log.info("controller.events.client_gone_in_handshake")
        raise _client_disconnected() from None


def _client_disconnected() -> BaseException:
    """uvicorn's quiet "the client left" exception (its run loop expects it)."""
    try:
        from uvicorn.protocols.utils import ClientDisconnected
    except ImportError:  # another ASGI server: a normal disconnect
        return WebSocketDisconnect(1006)
    return ClientDisconnected()


async def _next(sub: Subscriber, handle: StreamAuth | None) -> Any:
    """The next queued event, or :class:`_EndedError` (overflow or revoked)."""
    if sub.overflowed:
        raise _EndedError(CLOSE_SLOW_CONSUMER, "slow_consumer")
    if not sub.queue.empty():
        return sub.queue.get_nowait()
    waits: set[asyncio.Future[Any]] = {
        asyncio.ensure_future(sub.queue.get()),
        asyncio.ensure_future(sub.wake.wait()),
    }
    if handle is not None:
        waits.add(asyncio.ensure_future(handle.revoked.wait()))
    try:
        done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waits:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
    if handle is not None and handle.revoked.is_set():
        reason = handle.reason or StreamEndReason.UNAUTHORIZED
        code = (
            CLOSE_AUTH_UNAVAILABLE
            if reason == StreamEndReason.AUTH_UNAVAILABLE
            else CLOSE_UNAUTHORIZED
        )
        raise _EndedError(code, reason)
    if sub.overflowed:
        raise _EndedError(CLOSE_SLOW_CONSUMER, "slow_consumer")
    for waiter in done:
        if not waiter.cancelled() and waiter.exception() is None:
            value = waiter.result()
            if value is not True:  # the queue's get, not an Event.wait
                return value
    return None  # woken without an event: the caller loops


async def _follow(
    ws: WebSocket,
    bus: EventBus,
    sub: Subscriber,
    user: Principal,
    handle: StreamAuth | None,
) -> None:
    admin = user.is_admin
    while True:
        event = await _next(sub, handle)
        if event is None or not event.visible_to(user.user_id, admin=admin):
            continue
        await _send(ws, event.frame())


@event_router.websocket("/ws")
async def follow_events(
    websocket: WebSocket,
    since: Annotated[int | None, Query(ge=0, le=1 << 62)] = None,
    epoch: Annotated[str | None, Query(max_length=_MAX_EPOCH_CHARS)] = None,
) -> None:
    bus = getattr(websocket.app.state, EVENTS_STATE_KEY, None)
    try:
        user = await authenticate(websocket)
    except ApiError:  # the user database is down: fail closed
        user = None
    if user is None or not isinstance(bus, EventBus):
        await websocket.close(code=1008)  # before accept: the client sees 403
        return
    await websocket.accept()
    try:
        sub, resync = bus.subscribe(since=since, epoch=epoch)
    except OverflowError:
        await _close(websocket, CLOSE_TOO_MANY, "too_many_subscribers")
        return
    streams = get_auth(websocket).live_streams
    handle = None if streams is None else streams.open(user)
    try:
        if resync is None:
            hello: dict[str, Any] = {
                "type": "hello",
                "epoch": bus.epoch,
                "seq": bus.seq,
                "resumed": since is not None,
            }
        else:
            hello = {
                "type": "resync",
                "epoch": bus.epoch,
                "seq": bus.seq,
                "reason": resync.reason,
            }
        await _send_first(websocket, hello)
        await _until_gone(websocket, _follow(websocket, bus, sub, user, handle))
    except WebSocketDisconnect:
        return
    except _EndedError as ended:
        log.info(
            "controller.events.follower_closed",
            reason=ended.reason,
            user_id=user.user_id,
        )
        await _close(websocket, ended.code, ended.reason)
    finally:
        bus.unsubscribe(sub)
        if streams is not None and handle is not None:
            streams.close(handle)


async def _client_gone(ws: WebSocket) -> None:
    """Read (and ignore) what the client sends until it disconnects - the
    stream is server-to-client; this only notices a client that left."""
    while True:
        message = await ws.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(int(message.get("code") or 1000))


async def _until_gone(ws: WebSocket, work: Coroutine[Any, Any, None]) -> None:
    follow = asyncio.ensure_future(work)
    gone = asyncio.ensure_future(_client_gone(ws))
    try:
        await asyncio.wait({follow, gone}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (follow, gone):
            if not task.done():
                task.cancel()
        await asyncio.gather(follow, gone, return_exceptions=True)
    for task in (follow, gone):
        if task.done() and not task.cancelled() and task.exception() is not None:
            raise task.exception()  # type: ignore[misc]


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    if ws.application_state is not WebSocketState.CONNECTED:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(ws.close(code=code, reason=reason), _CLOSE_WAIT_S)
