# ruff: noqa: E501, T201  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (T-6): a hanging free-disk read, attacked with a REAL
blocked thread / a FIFO nobody writes (a syscall that never returns)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution import workdir_cap
from meshforge.agent.execution.workdir_cap import MAX_STUCK_READS, WorkdirCap

pytestmark = [pytest.mark.unit]


def _cap(
    tmp_path: Path, reader: Any, *, limit: int = 3, timeout_s: float = 0.05
) -> WorkdirCap:
    return WorkdirCap(
        tmp_path,
        max_bytes=10**12,
        interval_s=0.01,
        measure=lambda _p: 0,
        free_floor_bytes=100,
        disk_free=reader,
        max_free_read_failures=limit,
        free_read_timeout_s=timeout_s,
    )


def _run(cap: WorkdirCap, events: dict[str, Any]) -> asyncio.Task[Any]:
    return asyncio.create_task(
        cap.run(
            lambda w: events.setdefault("exceeded", w),
            lambda f: events.setdefault("low", f),
            lambda n: events.setdefault("unknown", n),
        )
    )


@pytest.fixture(autouse=True)
def _clean_counter() -> Any:
    yield
    # let any stuck reader of a test finish before the next test counts
    deadline = time.monotonic() + 5
    while workdir_cap._reads_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert workdir_cap._reads_running == 0


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo is POSIX-only")
async def test_a_fifo_read_that_never_returns_stops_the_attempt_disk_unknown(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    release = threading.Event()

    def reader(_root: Path) -> int:
        fd = os.open(fifo, os.O_RDONLY)  # blocks in open(2): nobody writes
        os.close(fd)
        release.wait()
        return 10**9

    events: dict[str, Any] = {}
    cap = _cap(tmp_path, reader, limit=4)
    task = _run(cap, events)
    try:
        await asyncio.wait_for(task, 10)
        assert events.get("unknown") == 4, events
    finally:
        # unblock the real syscall so the thread ends
        wfd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK) if _has_reader(fifo) else None
        release.set()
        if wfd is not None:
            os.close(wfd)
        _unblock(fifo)


def _has_reader(fifo: Path) -> bool:
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(fd)
    return True


def _unblock(fifo: Path) -> None:
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_streak_counts_through_hangs_and_resets_on_a_good_read(
    tmp_path: Path,
) -> None:
    gate = threading.Event()
    calls: list[int] = []

    def reader(_root: Path) -> int:
        calls.append(1)
        if len(calls) == 1:
            gate.wait()  # the first read hangs until released
        return 10**9  # healthy afterwards

    events: dict[str, Any] = {}
    cap = _cap(tmp_path, reader, limit=50, timeout_s=0.03)
    task = _run(cap, events)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if cap.free_read_failures >= 3:
            break
    assert cap.free_read_failures >= 3 and len(calls) == 1  # one read, still stuck
    gate.set()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if cap.free_read_failures == 0 and len(calls) > 1:
            break
    assert cap.free_read_failures == 0, "a good read must reset the streak"
    assert "unknown" not in events
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_threads_are_bounded_over_many_polls_and_many_watchers(
    tmp_path: Path,
) -> None:
    gate = threading.Event()

    def readers() -> int:
        return sum(1 for t in threading.enumerate() if t.name == "mf-free-disk")

    assert readers() == 0
    caps = [
        _cap(tmp_path, lambda _r: (gate.wait(), 10**9)[1], limit=10_000)
        for _ in range(30)
    ]
    events: dict[str, Any] = {}
    tasks = [_run(c, events) for c in caps]
    peak = 0
    for _ in range(100):  # ~1 s, thousands of polls
        await asyncio.sleep(0.01)
        peak = max(peak, readers())
    assert 1 <= peak <= MAX_STUCK_READS, peak
    assert all(c.free_read_failures > 0 for c in caps)  # nobody is silent
    gate.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_many_healthy_watchers_do_not_fail_each_other(tmp_path: Path) -> None:
    """MAX_STUCK_READS counts reads that are merely running: 64 watchers on a
    healthy disk polling at once must not see EBUSY failures (they would count
    towards disk_unknown)."""

    def reader(_root: Path) -> int:
        time.sleep(0.02)  # a slow but healthy network disk (20 ms a call)
        return 10**9

    caps = [_cap(tmp_path, reader, limit=10_000, timeout_s=1.0) for _ in range(12)]
    for cap in caps:
        cap.interval_s = 0.1
    events: dict[str, Any] = {}
    tasks = [_run(c, events) for c in caps]
    await asyncio.sleep(1.0)
    failed = sum(1 for c in caps if c.free_read_failures)
    worst = max(c.free_read_failures for c in caps)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert "unknown" not in events
    # report-level: how many healthy watchers saw at least one false failure
    print(
        f"healthy watchers with a counted failure at the end: {failed}/12 worst streak {worst}"
    )
    assert worst < 5


@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo is POSIX-only")
def test_the_process_exits_cleanly_with_a_read_stuck_in_a_syscall(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    script = textwrap.dedent(
        f"""
        import asyncio, os
        from pathlib import Path
        from meshforge.agent.execution.workdir_cap import WorkdirCap

        def reader(_r):
            fd = os.open({str(fifo)!r}, os.O_RDONLY)   # never returns
            return 1

        async def main():
            cap = WorkdirCap(Path({str(tmp_path)!r}), max_bytes=10**12, interval_s=0.01,
                measure=lambda p: 0, free_floor_bytes=100, disk_free=reader,
                max_free_read_failures=3, free_read_timeout_s=0.05)
            seen = []
            await asyncio.wait_for(cap.run(lambda w: None, lambda f: None, lambda n: seen.append(n)), 10)
            print("UNKNOWN", seen)

        asyncio.run(main())
        print("LOOP CLOSED")
        """
    )
    t0 = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-1500:]
    assert "UNKNOWN [3]" in done.stdout and "LOOP CLOSED" in done.stdout
    assert time.monotonic() - t0 < 15
