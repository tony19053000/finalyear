"""V1.0 G7 tester: try to break the D424 stall-aware lease timer.

Derived from the success criterion "a truly dead node still expires (max 2 grace
windows)". Real ``LeaseTimer.run`` loop, real sleeps (fast liveness config), a
jumpable clock to model controller stalls and clock jumps.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import time
from collections.abc import Callable
from typing import cast

import pytest
from test_session_manager import FakeRegistry, admit, heartbeat, hello, settle

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.manager import SessionManager

pytestmark = pytest.mark.unit

FAST = LivenessConfig(heartbeat_interval_s=0.2, miss_allowance_s=0.1)
OFFLINE = FAST.offline_after_s


class JumpClock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return asyncio.get_running_loop().time() + self.offset


def mgr(clock: JumpClock) -> SessionManager:
    return SessionManager(
        cast(Registry, FakeRegistry()),
        FAST,
        clock=clock,
        wall_clock=lambda: dt.datetime(2026, 10, 29, tzinfo=dt.UTC),
    )


async def until(cond: Callable[[], bool], within_s: float = 3.0) -> None:
    end = time.monotonic() + within_s
    while not cond():
        assert time.monotonic() < end, "condition never became true"
        await asyncio.sleep(0.005)


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_dead_node_stalls_repeatedly_still_expires_within_two_deferrals() -> None:
    """A stall at EVERY wake-up (clock jumps again during each listen window)."""
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    live = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        clock.offset += OFFLINE + 1
        while not live.ended:
            if timer.stall_deferrals < 10:
                clock.offset += 5.0  # keeps stalling during every listen window
            await asyncio.sleep(0.01)
            if timer.stall_deferrals > 2:
                break
        await until(lambda: live.ended)
        await settle()
        assert live.end_reason is SessionEndReason.LEASE_EXPIRED
        assert timer.stall_deferrals <= 2  # never more than two grace windows
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_expiry_delay_after_a_stall_is_bounded_by_two_intervals() -> None:
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    live = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        t0 = time.monotonic()
        clock.offset += OFFLINE + 1
        await until(lambda: live.ended)
        took = time.monotonic() - t0
        # 2 listen windows of one interval plus generous scheduling slack
        assert took < 2 * FAST.heartbeat_interval_s + 1.0, took
        # one real stall = one grace window, then the silent node is judged
        assert 1 <= timer.stall_deferrals <= 2
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_live_node_survives_a_stall_then_dead_after_is_expired() -> None:
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    live = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        clock.offset += OFFLINE + 1
        await until(lambda: timer.stall_deferrals == 1)
        for i in range(8):  # steady heartbeats after the stall
            await m.handle_message(live, heartbeat(live, f"hb{i}", seq=i + 1))
            await asyncio.sleep(0.05)
        assert live.state is NodeState.ONLINE and not live.ended
        # now the node really dies (no more heartbeats) with no stall
        await until(lambda: live.ended, within_s=5.0)
        assert live.end_reason is SessionEndReason.LEASE_EXPIRED
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_second_stall_episode_gets_a_fresh_grace_budget() -> None:
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    live = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        for i in range(3):
            clock.offset += OFFLINE + 1
            await until(lambda n=i: timer.stall_deferrals > n)
            await m.handle_message(live, heartbeat(live, f"hb{i}", seq=i + 1))
            ticks = timer.ticks
            await until(lambda t=ticks: timer.ticks > t)  # episode over: judged
        assert not live.ended
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_cancel_during_listen_window_is_clean() -> None:
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    await until(lambda: timer.ticks >= 1)
    clock.offset += OFFLINE + 1
    await until(lambda: timer.stall_deferrals == 1)
    t0 = time.monotonic()
    await stop(task)  # shutdown while listening: prompt, no hang, no exception leak
    assert time.monotonic() - t0 < 0.15
    assert task.cancelled()


@pytest.mark.asyncio
async def test_backward_clock_jump_never_defers_and_never_crashes() -> None:
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    live = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        clock.offset -= 1000.0
        ticks = timer.ticks
        await asyncio.sleep(0.2)
        assert timer.stall_deferrals == 0
        assert not task.done()
        assert not live.ended
        clock.offset += 1000.0 + 0.0  # back to normal
        await asyncio.sleep(0.1)
        assert not task.done()
        assert timer.ticks >= ticks
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_forward_jump_with_nothing_due_does_not_defer() -> None:
    """Late wake-up but no lease event due (heartbeats are flowing): no grace."""
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    live = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        for i in range(8):
            await m.handle_message(live, heartbeat(live, f"h{i}", seq=i + 1))
            await asyncio.sleep(0.05)
        assert timer.stall_deferrals == 0 and not live.ended
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_mixed_nodes_dead_one_expires_live_one_stays_after_stall() -> None:
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05)
    dead = await admit(m, hello(new_node_id()))
    alive = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        clock.offset += OFFLINE + 1
        await until(lambda: timer.stall_deferrals == 1)
        seq = 1
        while not dead.ended:
            seq += 1
            await m.handle_message(alive, heartbeat(alive, f"a{seq}", seq=seq))
            await asyncio.sleep(0.04)
            if seq > 200:
                break
        assert dead.ended and dead.end_reason is SessionEndReason.LEASE_EXPIRED
        assert not alive.ended and alive.state is NodeState.ONLINE
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_zero_deferrals_restores_immediate_expiry() -> None:
    clock = JumpClock()
    m = mgr(clock)
    timer = LeaseTimer(m, clock=clock, max_idle_s=0.05, max_stall_deferrals=0)
    live = await admit(m, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await until(lambda: timer.ticks >= 1)
        clock.offset += OFFLINE + 1
        await until(lambda: live.ended, within_s=1.0)
        assert timer.stall_deferrals == 0
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_stall_deferral_is_bounded_for_any_random_sequence() -> None:
    clock = JumpClock()
    m = mgr(clock)
    await admit(m, hello(new_node_id()))
    timer = LeaseTimer(m, clock=clock)
    rng = random.Random(1234)  # noqa: S311 - deterministic test data
    base = clock()
    for _ in range(2000):
        now = base + rng.uniform(0, 50)
        expected = None if rng.random() < 0.1 else now - rng.uniform(-3, 10)
        deferred = rng.randint(0, 6)
        got = timer.stall_deferral(now, expected, deferred)
        assert got in (0.0, FAST.heartbeat_interval_s)
        if deferred >= 2:
            assert got == 0.0
