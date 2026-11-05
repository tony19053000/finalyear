"""V1.0 G9-D tester: the renew loop's error handling is narrow - it swallows
send errors, never cancellation, and does not renew a lane that holds nothing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import test_v1_0_g9d_long_chunk as base  # noqa: F401  (shared fakes)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_cancellation_inside_a_send_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = base._FakeTime()
    monkeypatch.setattr(
        base.lane_module, "asyncio", base.SimpleNamespace(sleep=fake.sleep)
    )
    started = asyncio.Event()

    async def send_now(name: str, payload: Any) -> bool:
        started.set()
        await asyncio.Event().wait()  # a write that never returns
        return True

    host = base._Host(send_now)
    task = asyncio.ensure_future(host._renew_loop(base._attempt()))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_nothing_held_sends_nothing_and_a_permanent_error_never_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = base._FakeTime()
    monkeypatch.setattr(
        base.lane_module, "asyncio", base.SimpleNamespace(sleep=fake.sleep)
    )
    calls = 0

    async def send_now(name: str, payload: Any) -> bool:
        nonlocal calls
        calls += 1
        raise ConnectionResetError("gone")

    host = base._Host(send_now)
    attempt = base._attempt()
    attempt.lane.held.clear()
    task = asyncio.ensure_future(host._renew_loop(attempt))
    for _ in range(50):
        await asyncio.sleep(0)
    assert calls == 0
    attempt.lane.held[(41, 1)] = 0
    for _ in range(50):
        await asyncio.sleep(0)
    assert calls > 3 and not task.done()  # keeps trying every tick
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
