"""V0.7 G6 fix round 1 (D162, T-6): a free-space read that hangs.

* a hung read counts as a failed read at every poll (the D161 N1 streak), so
  the attempt stops ``disk_unknown`` after the configured streak;
* a watcher never starts a second read while its first is stuck: one thread
  per watcher, however long it runs;
* the process holds at most ``MAX_STUCK_READS`` such threads: more watchers on
  the same stale mount fail their reads at once (still counted) instead of
  starting threads;
* a read that is only slow, and then answers, resets the streak;
* the threads are daemons and end when the read returns; the setting exists
  and is bounded.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.agent.execution import workdir_cap
from meshforge.agent.execution.workdir_cap import MAX_STUCK_READS, WorkdirCap
from meshforge.agent.settings import ExecutionSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _cap(tmp_path: Path, reader: Any, *, limit: int, timeout_s: float) -> WorkdirCap:
    return WorkdirCap(
        tmp_path,
        max_bytes=10**12,
        interval_s=0.005,
        measure=lambda _p: 0,
        free_floor_bytes=100,
        disk_free=reader,
        max_free_read_failures=limit,
        free_read_timeout_s=timeout_s,
    )


async def _watch(cap: WorkdirCap, wait_s: float) -> dict[str, Any]:
    events: dict[str, Any] = {}
    task = asyncio.create_task(
        cap.run(
            lambda w: events.setdefault("exceeded", w),
            lambda f: events.setdefault("low", f),
            lambda n: events.setdefault("unknown", n),
        )
    )
    await asyncio.wait({task}, timeout=wait_s)
    events["returned"] = task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return events


def _disk_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "mf-free-disk"]


async def _settle() -> None:
    for _ in range(200):
        if not _disk_threads():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("free-disk reader threads did not end")


async def test_a_hung_read_is_counted_each_poll_with_one_thread(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    calls: list[int] = []

    def hang(_root: Path) -> int:
        calls.append(1)
        release.wait(30)
        return 10**12

    try:
        cap = _cap(tmp_path, hang, limit=4, timeout_s=0.05)
        events = await _watch(cap, 3.0)
        assert events.get("unknown") == 4, events
        assert events["returned"]
        assert len(calls) == 1, "a second read was started while the first hung"
        (thread,) = _disk_threads()
        assert thread.daemon
    finally:
        release.set()
    await _settle()
    assert workdir_cap._reads_running == 0


async def test_stuck_threads_are_bounded_for_the_whole_process(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    started: list[int] = []

    def hang(_root: Path) -> int:
        started.append(1)
        release.wait(30)
        return 10**12

    try:
        caps = [_cap(tmp_path, hang, limit=3, timeout_s=0.05) for _ in range(12)]
        results = await asyncio.gather(*[_watch(cap, 3.0) for cap in caps])
        # Every watcher stopped fail-closed - also those that could not start
        # a read because the bound was reached.
        assert [r.get("unknown") for r in results] == [3] * 12
        assert len(started) == MAX_STUCK_READS
        assert len(_disk_threads()) == MAX_STUCK_READS
    finally:
        release.set()
    await _settle()
    assert workdir_cap._reads_running == 0
    # Once the mount answers again, reads work again.
    cap = _cap(tmp_path, lambda _r: 5, limit=3, timeout_s=0.5)
    assert (await _watch(cap, 2.0)).get("low") == 5


async def test_a_slow_read_that_answers_resets_the_streak(tmp_path: Path) -> None:
    release = threading.Event()
    calls: list[int] = []

    def slow_then_fine(_root: Path) -> int:
        calls.append(1)
        if len(calls) == 1:
            release.wait(30)
        return 10**12

    cap = _cap(tmp_path, slow_then_fine, limit=50, timeout_s=0.02)
    task = asyncio.create_task(
        cap.run(lambda _w: None, lambda _f: None, lambda _n: None)
    )
    try:
        for _ in range(300):
            if cap.free_read_failures >= 3:
                break
            await asyncio.sleep(0.01)
        assert cap.free_read_failures >= 3
        release.set()
        for _ in range(300):
            if cap.free_read_failures == 0 and len(calls) > 1:
                break
            await asyncio.sleep(0.01)
        assert cap.free_read_failures == 0
        assert cap.free == 10**12
        assert not task.done()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await _settle()


async def test_the_timeout_setting_is_bounded() -> None:
    assert ExecutionSettings().free_disk_read_timeout_s == 2.0
    for bad in (0, 0.01, 61):
        with pytest.raises(ValidationError):
            ExecutionSettings(free_disk_read_timeout_s=bad)
    assert ExecutionSettings(free_disk_read_timeout_s=0.05)


async def test_a_job_on_a_hung_disk_fails_disk_unknown_with_the_configured_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor end to end: ``shutil.disk_usage`` hangs for the workdir's
    disk; the attempt fails ``disk_full`` / ``disk_unknown`` instead of running
    on, and the watch got ``[agent.execution] free_disk_read_timeout_s``."""
    import shutil

    import test_v07_g6_disk_unknown as base

    from meshforge.agent.execution import executor as executor_module
    from meshforge.agent.settings import AgentSettings
    from meshforge.common.errors import FailureType
    from meshforge.common.proto_codec import failure_type_from_proto

    real_load = AgentSettings.load

    def load(**kw: Any) -> AgentSettings:
        kw["agent"]["execution"]["free_disk_read_timeout_s"] = 0.07
        return real_load(**kw)

    monkeypatch.setattr(AgentSettings, "load", staticmethod(load))
    seen: list[float] = []
    real_cap = executor_module.WorkdirCap

    def spy(*args: Any, **kw: Any) -> WorkdirCap:
        seen.append(kw["free_read_timeout_s"])
        return real_cap(*args, **kw)

    monkeypatch.setattr(executor_module, "WorkdirCap", spy)
    reports = base.Reports()
    executor, msg = base._executor(tmp_path, base.SlowRuntime(30.0), reports)
    release = threading.Event()
    real_usage = shutil.disk_usage

    def hang(path: Any) -> Any:
        if Path(path).is_relative_to(executor.work_root):
            release.wait(60)
        return real_usage(path)

    monkeypatch.setattr(shutil, "disk_usage", hang)
    try:
        executor.handle(msg)
        await base._settle(executor)
        final = reports.sent[-1]
        assert failure_type_from_proto(final.failure.failure_type) is (
            FailureType.DISK_FULL
        )
        assert final.failure.message.startswith("disk_unknown:"), final.failure
        assert final.failure.retryable is False
        assert seen == [0.07]
        assert len(_disk_threads()) == 1
    finally:
        release.set()
        await executor.close()
    await _settle()
