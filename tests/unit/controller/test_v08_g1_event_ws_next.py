"""V0.8 G1 (D172): the event WebSocket's wait ends a slow or revoked follower
instead of letting it grow memory or outlive its token."""

from __future__ import annotations

import asyncio

import pytest

from meshforge.controller.api.event_routes import (
    CLOSE_AUTH_UNAVAILABLE,
    CLOSE_SLOW_CONSUMER,
    CLOSE_UNAUTHORIZED,
    _EndedError,
    _next,
)
from meshforge.controller.auth.live_streams import StreamAuth, StreamEndReason
from meshforge.controller.events.bus import EventBus, EventType

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_an_overflowed_follower_is_ended_slow_consumer() -> None:
    bus = EventBus(queue_size=2)
    sub, _ = bus.subscribe()
    for i in range(5):
        bus.publish(EventType.NODE_STATUS, {"i": i})
    with pytest.raises(_EndedError) as ended:
        await _next(sub, None)
    assert ended.value.code == CLOSE_SLOW_CONSUMER


async def test_an_overflow_while_waiting_wakes_the_follower() -> None:
    bus = EventBus(queue_size=1)
    sub, _ = bus.subscribe()
    first = bus.publish(EventType.NODE_STATUS, {"i": 0})
    assert await _next(sub, None) == first
    waiter = asyncio.ensure_future(_next(sub, None))
    await asyncio.sleep(0)
    sub.overflowed = True
    sub.wake.set()
    with pytest.raises(_EndedError):
        await asyncio.wait_for(waiter, 1)


@pytest.mark.security
@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (StreamEndReason.UNAUTHORIZED, CLOSE_UNAUTHORIZED),
        (StreamEndReason.AUTH_UNAVAILABLE, CLOSE_AUTH_UNAVAILABLE),
    ],
)
async def test_a_revoked_token_ends_the_wait(reason: str, code: int) -> None:
    bus = EventBus()
    sub, _ = bus.subscribe()
    handle = StreamAuth(token_id="tok_x", user_id="usr_x")
    waiter = asyncio.ensure_future(_next(sub, handle))
    await asyncio.sleep(0)
    handle.end(reason)
    with pytest.raises(_EndedError) as ended:
        await asyncio.wait_for(waiter, 1)
    assert ended.value.code == code
