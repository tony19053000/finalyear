"""V1.0 G10 (D444) tester: LoopWatch under real stalls, shutdown, rate limit."""

from __future__ import annotations

import asyncio
import builtins
import gc
import sys
import threading
import time

import pytest
import structlog

from meshforge.agent import loop_watch as lw
from meshforge.agent.loop_watch import LoopWatch, available_memory_mb

pytestmark = pytest.mark.unit


def _events(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e["event"] == "agent.loop.stalled"]


@pytest.mark.asyncio
async def test_repeated_real_stalls_are_rate_limited_and_counted() -> None:
    loop = asyncio.get_running_loop()
    watch = LoopWatch(
        clock=loop.time,
        interval_s=0.02,
        threshold_s=0.05,
        log_every_s=30.0,
        memory_mb=lambda: 123,
    )
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(watch.run())
        await asyncio.sleep(0.05)
        for _ in range(4):
            time.sleep(0.15)  # noqa: ASYNC251 - the stall under test
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert watch.stalls >= 4
    lines = _events(logs)
    assert len(lines) == 1  # one line per log_every_s, not one per stall
    assert lines[0]["available_mb"] == 123
    assert lines[0]["lag_s"] >= 0.1
    assert watch._suppressed == watch.stalls - 1


def test_suppressed_counts_roll_into_next_line() -> None:
    w = LoopWatch(clock=lambda: 0.0, threshold_s=1.0, log_every_s=10.0, memory_mb=lambda: None)
    with structlog.testing.capture_logs() as logs:
        w.observe(0, 2)  # logged
        for t in (3, 4, 5):
            w.observe(t - 3.0 + 0, t + 0.0 + (t - 3 + 1))  # lag grows
        w.observe(20, 25)  # beyond 10 s -> logged
    first, second = _events(logs)
    assert first["suppressed"] == 0 and first["worst_suppressed_s"] is None
    assert second["suppressed"] == 3
    assert second["worst_suppressed_s"] >= 2.0
    w.observe(40, 43)  # logged again; counters were reset
    assert w._suppressed == 0


@pytest.mark.asyncio
async def test_cancel_is_clean_and_nothing_logs_after_stop() -> None:
    loop = asyncio.get_running_loop()
    before = {t for t in asyncio.all_tasks()}
    watch = LoopWatch(clock=loop.time, interval_s=0.01, threshold_s=0.05, memory_mb=lambda: None)
    task = asyncio.create_task(watch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()
    assert {t for t in asyncio.all_tasks()} == before
    with structlog.testing.capture_logs() as logs:
        time.sleep(0.2)  # noqa: ASYNC251 - would be a stall if still running
        await asyncio.sleep(0.1)
    assert _events(logs) == []
    assert watch.stalls == 0


@pytest.mark.asyncio
async def test_cancel_during_a_stall_does_not_log_or_raise_garbage() -> None:
    loop = asyncio.get_running_loop()
    watch = LoopWatch(clock=loop.time, interval_s=0.01, threshold_s=0.05, memory_mb=lambda: None)
    task = asyncio.create_task(watch.run())
    await asyncio.sleep(0.03)
    time.sleep(0.2)  # noqa: ASYNC251
    task.cancel()  # cancel lands on a task whose timer already expired
    with structlog.testing.capture_logs() as logs:
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() or task.done()
    assert len(_events(logs)) <= 1


def test_no_leaked_thread_or_task_objects() -> None:
    n = threading.active_count()

    async def go() -> None:
        w = LoopWatch(clock=asyncio.get_running_loop().time, interval_s=0.01)
        t = asyncio.create_task(w.run())
        await asyncio.sleep(0.03)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)

    asyncio.run(go())
    gc.collect()
    assert threading.active_count() == n  # the watch uses no thread


def test_available_memory_is_none_when_psutil_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake(name: str, *a: object, **k: object) -> object:
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)
    assert available_memory_mb() is None


def test_available_memory_is_none_when_psutil_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    def boom() -> None:
        raise OSError("no /proc")

    monkeypatch.setattr(psutil, "virtual_memory", boom)
    assert available_memory_mb() is None


def test_a_stall_logs_even_when_memory_probe_is_none() -> None:
    w = LoopWatch(clock=lambda: 0.0, memory_mb=lambda: None)
    with structlog.testing.capture_logs() as logs:
        assert w.observe(0, 5) == 5
    (line,) = _events(logs)
    assert line["available_mb"] is None


def test_log_line_has_no_paths_or_secrets() -> None:
    w = LoopWatch(clock=lambda: 0.0, node_id="node_1", memory_mb=lambda: 5)
    with structlog.testing.capture_logs() as logs:
        w.observe(0, 5)
    (line,) = _events(logs)
    blob = repr(line)
    assert "/" not in blob.replace("(low free memory / paging, a blocked event loop)", "")
    assert set(line) <= {
        "event", "log_level", "node_id", "lag_s", "available_mb",
        "suppressed", "worst_suppressed_s", "hint",
    }


def test_max_lag_is_bounded_memory_and_correct_after_overflow() -> None:
    w = LoopWatch(clock=lambda: 0.0, threshold_s=0.1, log_every_s=1e9, memory_mb=lambda: None)
    for i in range(5000):
        w.observe(i, i + 0.5)
    assert len(w._stalls) == lw._KEEP
    assert w.max_lag_s(10.0, now=4999.5) == 0.5
    assert w.stalls == 5000


def test_overhead_of_a_quiet_tick_is_negligible() -> None:
    w = LoopWatch(clock=time.monotonic)
    n = 200_000
    t0 = time.perf_counter()
    for i in range(n):
        w.observe(i, i + 0.001)
    per_call = (time.perf_counter() - t0) / n
    assert per_call < 20e-6  # one call per 0.5 s in production: ~0 % of a core


@pytest.mark.asyncio
async def test_running_watch_adds_no_measurable_loop_latency() -> None:
    loop = asyncio.get_running_loop()
    watch = LoopWatch(clock=loop.time, interval_s=0.5)
    task = asyncio.create_task(watch.run())
    try:
        t0 = loop.time()
        for _ in range(2000):
            await asyncio.sleep(0)
        assert loop.time() - t0 < 1.0
        assert watch.stalls == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.xfail(
    strict=False,
    reason="D444 tester finding: max_lag_s excludes a stall whose watch wake-up has not run yet; a stream end processed first logs loop_lag_max_s=0.0 (race, ~20% here)",
)
@pytest.mark.asyncio
async def test_stall_is_visible_to_a_callback_that_runs_right_after_the_stall() -> None:
    """The session client reads max_lag_s when the stream end is processed. After
    a real stall the loop resumes; a threadsafe callback (what gRPC uses to wake
    the loop) queued *during* the stall runs in the first iteration after it.
    Does it already see the stall?"""
    loop = asyncio.get_running_loop()
    watch = LoopWatch(clock=loop.time, interval_s=0.02, threshold_s=0.1, memory_mb=lambda: None)
    task = asyncio.create_task(watch.run())
    seen: list[float] = []
    done = asyncio.Event()

    def poke() -> None:
        loop.call_soon_threadsafe(lambda: (seen.append(watch.max_lag_s(30.0)), done.set()))

    try:
        await asyncio.sleep(0.1)
        threading.Timer(0.1, poke).start()
        time.sleep(0.4)  # noqa: ASYNC251
        await asyncio.wait_for(done.wait(), 2)
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert watch.max_lag_s(30.0) >= 0.2
    # Informational: report what the first post-stall callback saw.
    assert seen, "callback never ran"
    assert seen[0] >= 0.2, f"stream-end-like callback saw lag {seen[0]} right after a 0.4 s stall"


# ------------------------------------------------------------ core_error / write_failed


def test_core_error_is_bounded_and_never_raises() -> None:
    import grpc

    from meshforge.agent.session.client import CORE_ERROR_MAX_CHARS, _core_error

    class Exc:
        def __init__(self, v: object) -> None:
            self.v = v

        def debug_error_string(self) -> object:
            if isinstance(self.v, Exception):
                raise self.v
            return self.v

    assert _core_error(Exc("é" * 10_000)) == "é" * CORE_ERROR_MAX_CHARS  # type: ignore[arg-type]
    assert _core_error(Exc(None)) == ""  # type: ignore[arg-type]
    assert _core_error(Exc(RuntimeError("x"))) == ""  # type: ignore[arg-type]
    assert grpc  # imported for parity with the client


def test_write_failed_ignores_other_internal_details_and_codes() -> None:
    import grpc

    from meshforge.agent.session.client import StreamEnd, write_failed
    from meshforge.agent.session.policy import StreamEndAction

    a = StreamEndAction.RECONNECT
    assert not write_failed(StreamEnd(grpc.StatusCode.INTERNAL, "Internal error from Core ", a))
    assert not write_failed(StreamEnd(grpc.StatusCode.UNKNOWN, "Internal error from Core", a))
    assert not write_failed(StreamEnd(grpc.StatusCode.INTERNAL, "", a))
