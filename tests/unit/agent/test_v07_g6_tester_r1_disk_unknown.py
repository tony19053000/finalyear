"""V0.7 G6 tester r1, item 0 (D161 N1): the free-disk read-failure streak.

From the brief: "count consecutive failures; after a configurable streak the
attempt FAILS disk_unknown ...; test both the streak and the reset".
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.agent.execution.workdir_cap import WorkdirCap
from meshforge.agent.settings import ExecutionSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BAD = object()


def _cap(script: list[Any], limit: int, tmp_path: Path, **kw: Any) -> WorkdirCap:
    it = iter(script)

    def reader(_root: Path) -> int:
        value = next(it, 10**12)
        if value is BAD:
            raise OSError("statvfs failed")
        if isinstance(value, Exception):
            raise value
        return int(value)

    return WorkdirCap(
        tmp_path,
        max_bytes=10**12,
        interval_s=0.005,
        measure=lambda _p: 0,
        free_floor_bytes=100,
        disk_free=reader,
        max_free_read_failures=limit,
        **kw,
    )


async def _run(cap: WorkdirCap, wait_s: float = 0.6) -> dict[str, Any]:
    events: dict[str, Any] = {}
    task = asyncio.create_task(
        cap.run(
            lambda w: events.setdefault("exceeded", w),
            lambda f: events.setdefault("low", f),
            lambda n: events.setdefault("unknown", n),
        )
    )
    await asyncio.wait({task}, timeout=wait_s)
    done = task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    events["returned"] = done
    return events


async def test_exactly_the_limit_in_a_row_stops_one_less_does_not(
    tmp_path: Path,
) -> None:
    for limit in (1, 2, 5):
        stopped = await _run(_cap([BAD] * limit, limit, tmp_path))
        assert stopped.get("unknown") == limit and stopped["returned"], limit
        if limit > 1:
            ok = await _run(_cap([BAD] * (limit - 1), limit, tmp_path))
            assert "unknown" not in ok and not ok["returned"], limit


async def test_a_good_read_resets_the_streak_every_time(tmp_path: Path) -> None:
    script: list[Any] = ([BAD] * 4 + [1000]) * 6
    events = await _run(_cap(script, 5, tmp_path), wait_s=0.8)
    assert "unknown" not in events and not events["returned"]


async def test_failures_of_every_exception_class_count(tmp_path: Path) -> None:
    script: list[Any] = [OSError("a"), ValueError("b"), RuntimeError("c")]
    events = await _run(_cap(script, 3, tmp_path))
    assert events.get("unknown") == 3


async def test_a_disk_below_the_floor_wins_over_a_pending_streak(
    tmp_path: Path,
) -> None:
    events = await _run(_cap([BAD, BAD, 5, BAD], 3, tmp_path))
    assert events.get("low") == 5 and "unknown" not in events


async def test_the_callback_fires_once_and_the_watch_returns(tmp_path: Path) -> None:
    calls: list[int] = []
    cap = _cap([BAD] * 50, 2, tmp_path)
    await asyncio.wait_for(cap.run(lambda _w: None, lambda _f: None, calls.append), 5)
    assert calls == [2]


async def test_a_limit_of_zero_still_needs_one_failure_not_none(tmp_path: Path) -> None:
    events = await _run(_cap([1000, 1000, 1000], 0, tmp_path))
    assert "unknown" not in events  # no failure -> no stop, even with limit 0


async def test_the_configured_limit_is_bounded() -> None:
    assert ExecutionSettings().free_disk_read_failure_limit == 5
    for bad in (0, -1, 1001):
        with pytest.raises(ValidationError):
            ExecutionSettings(free_disk_read_failure_limit=bad)
    for good in (1, 1000):
        assert ExecutionSettings(free_disk_read_failure_limit=good)


@pytest.mark.xfail(
    strict=True,
    reason="T-6 (minor): a free-space read that HANGS (stale network mount) "
    "is neither a failure nor a stop: there is no timeout around statvfs, so "
    "the watch waits forever and the streak never counts",
)
async def test_a_hanging_free_space_read_counts_as_a_failure(tmp_path: Path) -> None:
    def hang(_root: Path) -> int:
        time.sleep(3)
        return 10**12

    cap = WorkdirCap(
        tmp_path,
        max_bytes=10**12,
        interval_s=0.005,
        measure=lambda _p: 0,
        free_floor_bytes=100,
        disk_free=hang,
        max_free_read_failures=2,
    )
    events = await _run(cap, wait_s=1.5)
    assert events.get("unknown"), events
