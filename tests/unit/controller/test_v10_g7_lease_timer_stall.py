"""V1.0 G7 (D424): the lease timer does not judge a lease right after its own stall.

Unit level, fake clock: :meth:`LeaseTimer.stall_deferral` decides "listen first"
only when the timer woke up late **and** a lease event is due, at most
``max_stall_deferrals`` times in a row; a heartbeat read while listening keeps the
node ONLINE, and a really silent node is still expired afterwards. The real-loop
reproduction (a blocking 1.3 s stall with a live agent) is
``tests/component/agent/test_v10_g7_loop_stall_keeps_online.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import cast

import pytest
import structlog
from test_session_manager import (
    CFG,
    NOW0,
    FakeClock,
    FakeRegistry,
    admit,
    heartbeat,
    hello,
    make_manager,
    settle,
)

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.session.leases import STALL_TOLERANCE_SHARE, LeaseTimer
from meshforge.controller.session.manager import SessionManager

pytestmark = pytest.mark.unit

INTERVAL = CFG.heartbeat_interval_s  # 2.0 s
TOLERANCE = STALL_TOLERANCE_SHARE * INTERVAL


@pytest.mark.asyncio
async def test_no_deferral_without_a_scheduled_wake_up_or_when_on_time() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    await admit(manager, hello(new_node_id()))
    late_now = NOW0 + CFG.offline_after_s + 1.0  # an event is due
    assert timer.lease_event_due(late_now)
    assert timer.stall_deferral(late_now, None, 0) == 0.0  # first tick: no plan
    # woke up on time (or within the jitter tolerance): judge now
    assert timer.stall_deferral(late_now, late_now, 0) == 0.0
    assert timer.stall_deferral(late_now, late_now - TOLERANCE / 2, 0) == 0.0
    # woke up early (``wake`` was set): never a stall
    assert timer.stall_deferral(late_now, late_now + 5.0, 0) == 0.0


@pytest.mark.asyncio
async def test_a_late_wake_up_defers_only_when_a_lease_event_is_due() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    await admit(manager, hello(new_node_id()))
    quiet_now = NOW0 + 1.0  # inside the lease: nothing due
    assert not timer.lease_event_due(quiet_now)
    assert timer.stall_deferral(quiet_now, quiet_now - 0.9, 0) == 0.0

    due_now = NOW0 + CFG.suspect_after_s
    expected = due_now - 2 * TOLERANCE
    assert timer.stall_deferral(due_now, expected, 0) == INTERVAL
    assert timer.stall_deferral(due_now, expected, 1) == INTERVAL
    # bounded: after max_stall_deferrals (2) in a row the leases are judged
    assert timer.stall_deferral(due_now, expected, 2) == 0.0


@pytest.mark.asyncio
async def test_lease_event_due_ignores_ended_sessions_and_never_mutates() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    live = await admit(manager, hello(new_node_id()))
    late = NOW0 + 60.0
    for _ in range(3):
        assert timer.lease_event_due(late)  # read-only: still due every time
    assert live.state is NodeState.ONLINE
    await manager.end_session(live, SessionEndReason.AGENT_CLOSED)
    assert not timer.lease_event_due(late)


def test_constructor_validates_the_stall_settings() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    with pytest.raises(ValueError, match="stall_tolerance_s"):
        LeaseTimer(manager, clock=clock, stall_tolerance_s=-0.1)
    with pytest.raises(ValueError, match="max_stall_deferrals"):
        LeaseTimer(manager, clock=clock, max_stall_deferrals=-1)
    LeaseTimer(manager, clock=clock, stall_tolerance_s=0.0, max_stall_deferrals=0)


# ------------------------------------------------- run(): the loop end to end
#: Real sleeps in run(): a fast config keeps the listening interval short.
FAST = LivenessConfig(heartbeat_interval_s=0.2, miss_allowance_s=0.1)


def _fast_manager(registry: FakeRegistry, clock: _JumpClock) -> SessionManager:
    return SessionManager(
        cast(Registry, registry),
        FAST,
        clock=clock,
        wall_clock=lambda: dt.datetime(2026, 10, 29, tzinfo=dt.UTC),
    )


class _JumpClock:
    """``loop.time()`` plus a manual offset: a "stall" is a jump of the offset."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return asyncio.get_running_loop().time() + self.offset


async def _until(cond: object, within_s: float = 2.0) -> None:
    assert callable(cond)
    deadline = time.monotonic() + within_s
    while not cond():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_run_listens_after_a_stall_and_a_heartbeat_then_keeps_the_node() -> None:
    reg, clock = FakeRegistry(), _JumpClock()
    manager = _fast_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock, max_idle_s=0.05)
    live = await admit(manager, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await _until(lambda: timer.ticks >= 1)
        with structlog.testing.capture_logs() as logs:
            clock.offset += FAST.offline_after_s + 1.0  # the controller "stalled"
            await _until(lambda: timer.stall_deferrals == 1)
            # the heartbeat that waited in the socket is read while listening
            await manager.handle_message(live, heartbeat(live, "after-stall"))
            ticks = timer.ticks
            await _until(lambda: timer.ticks > ticks)  # judged after listening
        assert live.state is NodeState.ONLINE and not live.ended
        (entry,) = [e for e in logs if e["event"] == "lease_timer.stall_deferred"]
        assert entry["log_level"] == "warning" and entry["deferral"] == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_still_expires_a_really_silent_node_after_the_deferrals() -> None:
    reg, clock = FakeRegistry(), _JumpClock()
    manager = _fast_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock, max_idle_s=0.05, max_stall_deferrals=1)
    live = await admit(manager, hello(new_node_id()))
    task = asyncio.create_task(timer.run())
    try:
        await _until(lambda: timer.ticks >= 1)
        clock.offset += FAST.offline_after_s + 1.0
        await _until(lambda: timer.stall_deferrals == 1)
        assert live.state is NodeState.ONLINE  # not judged yet
        await _until(lambda: live.ended)
        await settle()
        assert live.end_reason is SessionEndReason.LEASE_EXPIRED
        assert timer.stall_deferrals == 1  # bounded: no second deferral
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
