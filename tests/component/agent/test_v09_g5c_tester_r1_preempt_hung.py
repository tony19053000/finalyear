"""V0.9 G5c tester r1 (items 0.2/0.3): a revert (``tc``) that never returns
while the owner-activity poll keeps firing - every pre-emption call stays
bounded, the shielded reverts do not pile up unboundedly, the marker stays,
and once the revert returns everything is cleaned (no leaked burner)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the agent extra")

from test_v09_g4_conditions_exec import (  # type: ignore[import-not-found]
    GatedRuntime,
    Reports,
    _burners,
    _executor,
    _no_burners,
    _settle,
    _start,
)
from test_v09_g5b_owner_preempt_exec import (  # type: ignore[import-not-found]
    _bench,
    _final,
    _state,
)

from meshforge.common.bench_conditions import ConditionKind

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]


async def _wait_for(predicate: Any, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_repeated_poll_preemptions_with_a_hung_revert_stay_bounded(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    bound = 0.3
    executor.preempt_revert_timeout_s = bound
    await executor.start()
    bench = _bench(_start(digest, "cpu_load:1"))
    attempt_id = bench.start_attempt.attempt_id
    executor.handle(bench)
    await asyncio.wait_for(runtime.running.wait(), 30)
    (condition,) = executor.conditions.active
    controller = condition.controller
    real_revert = controller.revert
    gate = asyncio.Event()
    calls = 0

    async def hung_revert() -> bool:
        nonlocal calls
        calls += 1
        await gate.wait()
        return bool(await real_revert())

    controller.revert = hung_revert
    tasks_before = len(asyncio.all_tasks())
    worst = 0.0
    try:
        for _ in range(8):  # the 2 s poll, compressed
            t = time.monotonic()
            await asyncio.wait_for(executor.preempt_benchmarks("paused"), 10)
            worst = max(worst, time.monotonic() - t)
            await asyncio.sleep(0.05)
        assert worst < bound * 4 + 1.0, worst
        # shielded reverts must not pile up one per poll
        assert len(asyncio.all_tasks()) - tasks_before < 8, (
            len(asyncio.all_tasks()) - tasks_before
        )
        assert calls <= 3, calls  # one revert in flight, not one per poll
        assert attempt_id in executor.conditions.marker.load()
        assert len(_burners()) == 1  # still applied while the revert hangs
    finally:
        gate.set()
    await _wait_for(lambda: _state(_final(reports, attempt_id)) == "failed")
    await _wait_for(lambda: attempt_id not in executor.conditions.marker.load())
    await _no_burners()
    await _settle(executor)
    await executor.close()
