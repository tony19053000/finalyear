"""V0.9 G5 lane B tester round 3 (D251): the owner guard's pre-emption retry.

Driven with a fake clock and a fake activity (state + ``on_poll``): backoff
and the 30 s cap, a success resetting the backoff, the guard clearing
mid-backoff, the poll path never raising, one log per step, no storm under
1000 polls, a slow pre-emption racing the event path.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest
import structlog

from meshforge.agent.owner_activity import ActivityState
from meshforge.agent.owner_guard import MAX_RETRY_S, OwnerGuard

pytestmark = pytest.mark.unit


class Act:
    def __init__(self, poll_s: float = 2.0) -> None:
        self.state = ActivityState(active=True, reason="idle", idle_source="x")
        self.poll_s = poll_s
        self.on_poll: Any = None

    def set(self, active: bool) -> None:
        self.state = ActivityState(
            active=active, reason="idle" if active else "", idle_source="x"
        )


class Rig:
    def __init__(self, fail: int | float = 0, poll_s: float = 2.0) -> None:
        self.clock = 0.0
        self.act = Act(poll_s)
        self.fail = fail
        self.tries: list[float] = []
        self.guard = OwnerGuard(
            policy=None,
            activity=self.act,  # type: ignore[arg-type]
            preempt=self._preempt,
            clock=lambda: self.clock,
        )

    async def _preempt(self, _reason: str) -> int:
        self.tries.append(self.clock)
        if len(self.tries) <= self.fail:
            raise OSError("boom")
        return 1

    async def poll(self, advance: float | None = None) -> None:
        self.clock += self.act.poll_s if advance is None else advance
        await self.act.on_poll(self.act.state)  # never raises


@pytest.mark.asyncio
async def test_backoff_doubles_and_caps_at_30s_for_other_poll_intervals() -> None:
    for poll_s in (0.5, 2.0, 10.0, 40.0):
        rig = Rig(fail=10**9, poll_s=poll_s)
        with pytest.raises(OSError):
            await rig.guard.refresh()
        for _ in range(3000):
            await rig.poll(poll_s / 4)  # polls faster than the interval
        gaps = [round(b - a, 3) for a, b in itertools.pairwise(rig.tries)]
        assert gaps, poll_s
        # each gap is the wanted delay, rounded UP to the poll granularity
        assert all(g <= MAX_RETRY_S + poll_s / 4 + 1e-9 for g in gaps), (poll_s, gaps)
        if poll_s <= 10:
            assert max(gaps) >= MAX_RETRY_S, (poll_s, gaps)
        assert gaps == sorted(gaps), (poll_s, gaps)  # never shrinks


@pytest.mark.asyncio
async def test_success_after_n_failures_stops_retrying_and_resets_the_backoff() -> None:
    rig = Rig(fail=4)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    for _ in range(100):
        await rig.poll()
    assert len(rig.tries) == 5 and rig.guard.preempted == 1  # 4 failures + 1 success
    # clear, then a new turn-on: backoff starts from scratch (retry on next poll)
    rig.act.set(False)
    await rig.guard.refresh()  # the activity flip calls the guard (on_change)
    rig.act.set(True)
    rig.fail = len(rig.tries) + 2  # fail the next two
    with pytest.raises(OSError):
        await rig.guard.refresh()
    n = len(rig.tries)
    await rig.poll()  # 1st retry on the very next poll
    assert len(rig.tries) == n + 1
    await rig.poll()  # 2nd failure -> delay 2 polls: not now
    assert len(rig.tries) == n + 1
    await rig.poll()
    assert len(rig.tries) == n + 2 and rig.guard.preempted == 2


@pytest.mark.asyncio
async def test_the_guard_clearing_mid_backoff_stops_the_retries() -> None:
    rig = Rig(fail=10**9)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    for _ in range(6):
        await rig.poll()
    seen = len(rig.tries)
    assert seen >= 3
    rig.act.set(False)
    for _ in range(500):
        await rig.poll()
    assert len(rig.tries) == seen
    # and a fresh turn-on starts at the next poll (not the old retry_at)
    rig.act.set(True)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    await rig.poll()
    assert len(rig.tries) == seen + 2


@pytest.mark.asyncio
async def test_a_poll_after_the_guard_cleared_and_returned_before_any_poll() -> None:
    """Clear + re-activate between two polls: no stale state, still retried."""
    rig = Rig(fail=1)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    rig.act.set(False)
    await rig.guard.refresh()
    rig.act.set(True)
    await rig.guard.refresh()  # a new turn-on; this one succeeds
    assert rig.guard.preempted == 1
    for _ in range(50):
        await rig.poll()
    assert len(rig.tries) == 2


@pytest.mark.asyncio
async def test_poll_path_never_raises_for_a_custom_exception() -> None:
    class WeirdError(Exception):
        pass

    rig = Rig()

    async def bad(_r: str) -> int:
        raise WeirdError("x")

    rig.guard.preempt = bad
    with pytest.raises(WeirdError):
        await rig.guard.refresh()
    for _ in range(20):
        await rig.poll()  # no exception


@pytest.mark.asyncio
async def test_cancellation_inside_a_poll_retry_propagates() -> None:
    rig = Rig()

    async def cancelled(_r: str) -> int:
        raise asyncio.CancelledError

    rig.guard.preempt = cancelled
    with pytest.raises(asyncio.CancelledError):
        await rig.guard.refresh()
    # the event path left it pending; the poll retry must not swallow a cancel
    with pytest.raises(asyncio.CancelledError):
        await rig.poll()


@pytest.mark.asyncio
async def test_one_log_per_failed_step_and_none_between() -> None:
    rig = Rig(fail=10**9)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(OSError):
            await rig.guard.refresh()
        for _ in range(400):
            await rig.poll()
    failed = [e for e in logs if e["event"] == "agent.bench.preempt_failed"]
    assert len(failed) == len(rig.tries)
    assert [e["failures"] for e in failed] == list(range(1, len(rig.tries) + 1))
    assert all(e["retry_in_s"] <= MAX_RETRY_S for e in failed)
    others = [e for e in logs if e["event"] not in ("agent.bench.preempt_failed",)]
    assert not others


@pytest.mark.asyncio
async def test_1000_polls_inside_one_backoff_window_make_one_try() -> None:
    rig = Rig(fail=10**9)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    await rig.poll()  # 1st retry (fails) -> now waits 2 intervals (4 s)
    n = len(rig.tries)
    for _ in range(1000):
        await rig.poll(advance=0.001)  # 1 s of polls in total
    assert len(rig.tries) == n


@pytest.mark.asyncio
async def test_no_storm_with_a_frozen_clock() -> None:
    rig = Rig(fail=10**9)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    await rig.poll()
    n = len(rig.tries)
    for _ in range(1000):
        await rig.poll(advance=0.0)
    assert len(rig.tries) == n


@pytest.mark.asyncio
async def test_a_clock_that_goes_backwards_does_not_storm() -> None:
    rig = Rig(fail=10**9)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    await rig.poll()
    n = len(rig.tries)
    for _ in range(1000):
        await rig.poll(advance=-0.01)
    assert len(rig.tries) == n


@pytest.mark.asyncio
async def test_the_event_path_still_raises_during_backoff_and_counts_a_failure() -> (
    None
):
    rig = Rig(fail=10**9)
    with pytest.raises(OSError):
        await rig.guard.refresh()
    await rig.poll()
    with pytest.raises(OSError):
        await rig.guard.refresh()  # event path: always tries, always raises
    assert len(rig.tries) == 3


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="T3-1: poll-retry and event-path refresh can run two pre-emptions at once "
    "(double stop_all, 'preempted' counted twice)",
)
async def test_a_slow_preemption_racing_the_event_path_counts_once() -> None:
    """The activity loop (poll retry) and the policy watcher (refresh) run as
    two tasks; a slow pre-emption must not be run twice concurrently."""
    rig = Rig(fail=1)
    gate = asyncio.Event()
    inflight = 0
    peak = 0

    async def slow(_r: str) -> int:
        nonlocal inflight, peak
        rig.tries.append(rig.clock)
        if len(rig.tries) == 1:
            raise OSError("first")
        inflight += 1
        peak = max(peak, inflight)
        await gate.wait()
        inflight -= 1
        return 1

    rig.guard.preempt = slow
    with pytest.raises(OSError):
        await rig.guard.refresh()
    a = asyncio.create_task(rig.poll())
    await asyncio.sleep(0)
    b = asyncio.create_task(rig.guard.refresh())
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(a, b)
    assert peak == 1, "two pre-emptions ran at the same time"
    assert rig.guard.preempted == 1, "one turn-on counted twice"
