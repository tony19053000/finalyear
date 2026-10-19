"""V0.9 G5 lane B tester round 3 (D251 B2): ``Executor.preempt_benchmarks`` when
``stop_all`` raises, is cancelled, hangs, or races the attempt's natural end.

Invariant: after ``preempt_benchmarks`` returns or raises, no benchmark attempt
is left with ``stop_reason`` set while its step still runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the agent extra")

from test_v09_g4_conditions_exec import (  # type: ignore[import-not-found]
    GatedRuntime,
    Reports,
    _executor,
    _no_burners,
    _settle,
    _start,
)
from test_v09_g5b_owner_preempt_exec import (  # type: ignore[import-not-found]
    TwoRuntimes,
    _bench,
    _final,
    _state,
)

from meshforge.common.bench_conditions import ConditionKind
from meshforge.common.proto_codec import failure_type_from_proto

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]


def _leaks(executor: Any) -> list[str]:
    """Attempts with a stop_reason whose step is still running."""
    return [
        a.attempt_id
        for a in executor._attempts.values()
        if a.stop_reason is not None
        and not a.ended
        and a.step is not None
        and not a.step.done()
    ]


@pytest.mark.asyncio
async def test_stop_all_raising_still_stops_every_selected_bench_attempt(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    msgs = [_bench(_start(digest, None)) for _ in range(3)]
    for m in msgs:
        executor.handle(m)
    for _ in range(300):
        if runtime.calls == 3:
            break
        await asyncio.sleep(0.05)
    assert runtime.calls == 3

    async def boom(_reason: str) -> int:
        raise RuntimeError("revert failed")

    executor.conditions.stop_all = boom
    executor.conditions._active["x"] = object()
    try:
        with pytest.raises(RuntimeError, match="revert failed"):
            await executor.preempt_benchmarks("paused")
    finally:
        executor.conditions._active.clear()
    await _settle(executor)
    for m in msgs:
        final = _final(reports, m.start_attempt.attempt_id)
        assert _state(final) == "failed"
        assert (
            failure_type_from_proto(final.failure.failure_type).value
            == "owner_preempted"
        )
    assert not _leaks(executor)
    assert all(a.preempt_stopped for a in executor._attempts.values())
    executor.conditions.__dict__.pop("stop_all", None)  # the real one for close()
    await executor.close()


@pytest.mark.asyncio
async def test_stop_all_cancelled_midway_still_stops_the_attempts(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    bench = _bench(_start(digest, None))
    executor.handle(bench)
    await asyncio.wait_for(runtime.running.wait(), 30)
    entered = asyncio.Event()

    async def hang(_reason: str) -> int:
        entered.set()
        await asyncio.Event().wait()
        return 0

    executor.conditions.stop_all = hang
    executor.conditions._active["x"] = object()
    task = asyncio.create_task(executor.preempt_benchmarks("paused"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()  # e.g. the agent shuts down / the activity loop is cancelled
    with pytest.raises(asyncio.CancelledError):
        await task
    executor.conditions._active.clear()
    await _settle(executor)
    final = _final(reports, bench.start_attempt.attempt_id)
    assert (
        failure_type_from_proto(final.failure.failure_type).value == "owner_preempted"
    )
    assert not _leaks(executor)
    executor.conditions.__dict__.pop("stop_all", None)  # the real one for close()
    await executor.close()


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="T3-2: no timeout around stop_all - a hanging revert leaves the attempt "
    "marked owner_preempted but running, and blocks the activity poll loop",
)
async def test_a_hanging_stop_all_does_not_leave_the_attempt_running(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    bench = _bench(_start(digest, None))
    executor.handle(bench)
    await asyncio.wait_for(runtime.running.wait(), 30)

    async def hang(_reason: str) -> int:
        await asyncio.Event().wait()
        return 0

    executor.conditions.stop_all = hang
    executor.conditions._active["x"] = object()
    try:
        # a bounded pre-emption: the poll loop awaits it, it must come back
        await asyncio.wait_for(executor.preempt_benchmarks("paused"), 5)
        assert not _leaks(executor)
    finally:
        executor.conditions._active.clear()
        runtime.release.set()
        await _settle(executor)
        executor.conditions.__dict__.pop("stop_all", None)  # the real one for close()
    await executor.close()


@pytest.mark.asyncio
async def test_natural_finish_racing_the_preemption_is_consistent(
    tmp_path: Path,
) -> None:
    """The workload finishes while stop_all is in flight."""
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    bench = _bench(_start(digest, None))
    executor.handle(bench)
    await asyncio.wait_for(runtime.running.wait(), 30)
    entered = asyncio.Event()
    go = asyncio.Event()

    async def slow(_reason: str) -> int:
        entered.set()
        await go.wait()
        return 0

    executor.conditions.stop_all = slow
    executor.conditions._active["x"] = object()
    task = asyncio.create_task(executor.preempt_benchmarks("paused"))
    await asyncio.wait_for(entered.wait(), 5)
    runtime.release.set()  # the run ends naturally now
    for _ in range(100):
        await asyncio.sleep(0.01)
    go.set()
    n = await task
    executor.conditions._active.clear()
    await _settle(executor)
    final = _final(reports, bench.start_attempt.attempt_id)
    attempt = executor._attempts[bench.start_attempt.attempt_id]
    assert attempt.ended and not _leaks(executor)
    state = _state(final)
    # either it was stopped (failed/owner_preempted) or it finished (done);
    # never "done" together with a pre-emption count that claims it was stopped
    if state == "done":
        assert n == 0 or attempt.preempt_stopped is True
    else:
        assert (
            failure_type_from_proto(final.failure.failure_type).value
            == "owner_preempted"
        )
    # a retry never re-selects an ended attempt
    assert await executor.preempt_benchmarks("paused") == 0
    executor.conditions.__dict__.pop("stop_all", None)  # the real one for close()
    await executor.close()


@pytest.mark.asyncio
async def test_preempt_with_nothing_running_and_no_conditions_is_a_noop(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, _digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    calls: list[str] = []

    async def spy(reason: str) -> int:
        calls.append(reason)
        return 0

    executor.conditions.stop_all = spy
    for _ in range(5):
        assert await executor.preempt_benchmarks("paused") == 0
    assert calls == []
    executor.conditions.__dict__.pop("stop_all", None)  # the real one for close()
    await executor.close()


@pytest.mark.asyncio
async def test_user_attempt_is_never_selected_even_when_stop_all_raises(
    tmp_path: Path,
) -> None:
    runtime, reports = TwoRuntimes(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    await executor.start()
    bench = _bench(_start(digest, "cpu_load:1"))
    user = _start(digest, None)
    executor.handle(bench)
    executor.handle(user)
    await asyncio.wait_for(runtime.two.wait(), 30)

    async def boom(_r: str) -> int:
        raise OSError("x")

    real = executor.conditions.stop_all
    executor.conditions.stop_all = boom
    for _ in range(3):  # repeated retries while stop_all keeps raising
        with pytest.raises(OSError):
            await executor.preempt_benchmarks("paused")
    u = executor._attempts[user.start_attempt.attempt_id]
    assert u.stop_reason is None and not u.preempt_stopped
    executor.conditions.stop_all = real
    await executor.preempt_benchmarks("paused")
    await _no_burners()
    runtime.release.set()
    await _settle(executor)
    assert _state(_final(reports, user.start_attempt.attempt_id)) == "done"
    executor.conditions.__dict__.pop("stop_all", None)  # the real one for close()
    await executor.close()


@pytest.mark.asyncio
async def test_a_second_preemption_does_not_restop_an_already_stopped_attempt(
    tmp_path: Path,
) -> None:
    """``preempt_stopped`` distinguishes 'marked, never stopped' (retry picks it)
    from 'stopped, step cancel still in flight' (a repeat must not count it)."""
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    msg = _bench(_start(digest, None))
    executor.handle(msg)
    await asyncio.wait_for(runtime.running.wait(), 30)
    assert await executor.preempt_benchmarks("paused") == 1
    # no await between: the attempt has not ended yet
    assert await executor.preempt_benchmarks("paused") == 0
    await _settle(executor)
    assert await executor.preempt_benchmarks("paused") == 0
    final = _final(reports, msg.start_attempt.attempt_id)
    assert _state(final) == "failed"
    await executor.close()
