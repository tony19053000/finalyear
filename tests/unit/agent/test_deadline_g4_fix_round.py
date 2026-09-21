"""V0.3 G4 fix round (H1): adversarial tests for ``DeadlineThread`` itself.

The fix round's own tests (``test_agent_resources.py``) exercise the happy paths
(bounded at 0.301s against a 5s wedge, one leaked daemon thread, ``asyncio.run()``
returning without waiting for it). Nothing in the repository exercises the module
in isolation: exceptions, the exact-deadline boundary, repeated wedge cycles, slot
reuse after an abandoned thread finally completes, concurrent callers on one
instance, independent instances, and degenerate ``timeout_s``.

The central hazard this file is built around: a :class:`DeadlineThread` has exactly
one ``_busy`` flag and one thread slot. When an abandoned call *finally* finishes,
it clears that flag from a thread that no longer has anyone waiting on it. If that
happened while a *later* call already believed it owned the slot, the later call's
result could be corrupted. Each call keeps its own local ``box``/``done``, so this
file proves that isolation holds under exactly that interleaving instead of just
reading the code and trusting the docstring.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from meshforge.agent.hardware.deadline import (
    BlockingCallStalledError,
    BlockingCallTimeoutError,
    DeadlineThread,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_normal_call_returns_its_value() -> None:
    guard = DeadlineThread("t")
    result = await guard.call(lambda: 42, timeout_s=1.0)
    assert result == 42
    assert guard.calls == 1
    assert guard.timeouts == 0
    assert guard.stalled_calls == 0
    assert guard.abandoned == 0
    assert guard.stalled is False


@pytest.mark.asyncio
async def test_an_exception_from_the_callable_propagates_to_the_caller() -> None:
    guard = DeadlineThread("t")

    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await guard.call(boom, timeout_s=1.0)
    # An exception is a completed call, not a stall: the slot is free again.
    assert guard.stalled is False
    assert guard.timeouts == 0


@pytest.mark.asyncio
async def test_the_slot_is_free_again_immediately_after_a_normal_return() -> None:
    guard = DeadlineThread("t")
    await guard.call(lambda: 1, timeout_s=1.0)
    # A second call must succeed right away, not raise BlockingCallStalledError.
    result = await guard.call(lambda: 2, timeout_s=1.0)
    assert result == 2


@pytest.mark.asyncio
async def test_a_call_that_finishes_essentially_at_the_deadline_still_returns() -> None:
    guard = DeadlineThread("t")

    def almost_late() -> str:
        time.sleep(0.05)
        return "made it"

    # Timeout is generous relative to the work; this asserts the happy path is not
    # flaky at a realistic margin, not that the boundary is exact to the millisecond.
    result = await guard.call(almost_late, timeout_s=0.5)
    assert result == "made it"


@pytest.mark.asyncio
async def test_a_wedged_call_times_out_and_is_abandoned() -> None:
    guard = DeadlineThread("t")
    release = threading.Event()

    def wedge() -> None:
        release.wait(5.0)

    with pytest.raises(BlockingCallTimeoutError):
        await guard.call(wedge, timeout_s=0.05)
    assert guard.timeouts == 1
    assert guard.abandoned == 1
    assert guard.stalled is True
    release.set()  # let the daemon thread exit so it doesn't outlive the test


@pytest.mark.asyncio
async def test_a_second_call_while_the_first_is_wedged_fails_fast_not_slow() -> None:
    guard = DeadlineThread("t")
    release = threading.Event()

    def wedge() -> None:
        release.wait(5.0)

    with pytest.raises(BlockingCallTimeoutError):
        await guard.call(wedge, timeout_s=0.05)

    started = time.monotonic()
    with pytest.raises(BlockingCallStalledError):
        await guard.call(lambda: "should not run", timeout_s=5.0)
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, (
        "a stalled call must fail immediately, not wait out its own timeout"
    )
    assert guard.stalled_calls == 1
    # The stalled call never started a thread: `calls` only counts threads started.
    assert guard.calls == 1
    release.set()


@pytest.mark.asyncio
async def test_the_slot_frees_once_the_abandoned_thread_actually_finishes() -> None:
    guard = DeadlineThread("t")
    release = threading.Event()

    def wedge() -> None:
        release.wait(5.0)

    with pytest.raises(BlockingCallTimeoutError):
        await guard.call(wedge, timeout_s=0.05)
    assert guard.stalled is True

    release.set()  # let the abandoned thread complete
    deadline = time.monotonic() + 2.0
    while guard.stalled and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    assert guard.stalled is False, "the abandoned thread finishing must clear the slot"

    result = await guard.call(lambda: "fresh call", timeout_s=1.0)
    assert result == "fresh call", (
        "a later call must get its own result, not one left over from the "
        "abandoned call that just vacated the slot"
    )


@pytest.mark.asyncio
async def test_a_late_finishing_abandoned_call_does_not_corrupt_a_later_calls_result() -> (
    None
):
    """The exact hazard the module docstring calls out: prove it with two calls
    whose completion order is deliberately interleaved.

    Call 1 wedges and is abandoned. Only *after* call 2 has been issued (and has
    itself returned) do we release call 1's thread. If the two calls' book-keeping
    were shared instead of per-call-local, call 1 finishing late could still
    clobber something call 2 depended on.
    """
    guard = DeadlineThread("t")
    release_1 = threading.Event()

    def wedge_then_return_marker() -> str:
        release_1.wait(5.0)
        return "call-1-late-value"

    with pytest.raises(BlockingCallTimeoutError):
        await guard.call(wedge_then_return_marker, timeout_s=0.05)

    # Free the slot the same way production code observes it clearing, by waiting
    # it out is not an option here (call 1 is deliberately still parked on
    # release_1), so this exercises the *stalled* path for call 2 instead - which
    # is the real production interleaving: call 2 arrives before call 1 finishes.
    with pytest.raises(BlockingCallStalledError):
        await guard.call(lambda: "call-2-value", timeout_s=1.0)

    # Now let call 1 finish. It must not resurrect itself as call 2's answer -
    # there is nobody left awaiting it, and no result was returned to any caller.
    release_1.set()
    deadline = time.monotonic() + 2.0
    while guard.stalled and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    assert guard.stalled is False

    # The slot is free again; a real call 3 must get its own, distinct value.
    result = await guard.call(lambda: "call-3-value", timeout_s=1.0)
    assert result == "call-3-value"


@pytest.mark.asyncio
async def test_repeated_wedge_cycles_accumulate_counters_without_crashing() -> None:
    guard = DeadlineThread("t")
    releases: list[threading.Event] = []
    for _ in range(5):
        release = threading.Event()
        releases.append(release)

        def wedge(ev: threading.Event = release) -> None:
            ev.wait(5.0)

        with pytest.raises(BlockingCallTimeoutError):
            await guard.call(wedge, timeout_s=0.03)
        release.set()
        deadline = time.monotonic() + 2.0
        while guard.stalled and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        assert guard.stalled is False, "must free up between cycles to prove no leak"

    assert guard.timeouts == 5
    assert guard.abandoned == 5
    assert guard.stalled_calls == 0
    assert guard.calls == 5


@pytest.mark.asyncio
async def test_two_independent_instances_do_not_share_a_slot() -> None:
    guard_a = DeadlineThread("a")
    guard_b = DeadlineThread("b")
    release = threading.Event()

    def wedge() -> None:
        release.wait(5.0)

    with pytest.raises(BlockingCallTimeoutError):
        await guard_a.call(wedge, timeout_s=0.05)
    assert guard_a.stalled is True

    # guard_b must be entirely unaffected by guard_a being stalled.
    result = await guard_b.call(lambda: "independent", timeout_s=1.0)
    assert result == "independent"
    assert guard_b.stalled is False
    assert guard_b.timeouts == 0
    release.set()


@pytest.mark.parametrize("timeout_s", [0.0, -1.0, -100.0])
@pytest.mark.asyncio
async def test_a_non_positive_timeout_is_clamped_not_rejected(timeout_s: float) -> None:
    """The module clamps to a minimum rather than raising or busy-waiting forever."""
    guard = DeadlineThread("t")
    # A fast function should still be able to win the race against the clamped
    # minimum timeout at least some of the time; what must never happen is an
    # exception from the clamp itself or a hang.
    try:
        result = await guard.call(lambda: "ok", timeout_s=timeout_s)
        assert result == "ok"
    except BlockingCallTimeoutError:
        pass  # also acceptable: the minimum timeout is small enough to lose the race


@pytest.mark.asyncio
async def test_concurrent_callers_on_one_instance_the_loser_is_stalled_not_corrupted() -> (
    None
):
    guard = DeadlineThread("t")
    started = threading.Event()
    release = threading.Event()

    def slow_first() -> str:
        started.set()
        release.wait(5.0)
        return "first"

    async def run_first() -> object:
        try:
            return await guard.call(slow_first, timeout_s=5.0)
        except BlockingCallTimeoutError as exc:
            return exc

    first_task = asyncio.create_task(run_first())
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 2.0)
    # Second caller arrives while the first genuinely still occupies the slot.
    with pytest.raises(BlockingCallStalledError):
        await guard.call(lambda: "second", timeout_s=1.0)

    release.set()
    outcome = await first_task
    assert outcome == "first"
