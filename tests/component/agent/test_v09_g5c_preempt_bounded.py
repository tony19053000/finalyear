"""V0.9 G5c items 0.3 + 0.4 (D253 T3-2, D252.6): a pre-emption is bounded.

Real executor, real ``cpu_load:1`` burner, real condition runner and marker;
only the condition controller's ``revert`` is made to hang (a ``tc`` that
never returns). Checked:

* ``preempt_benchmarks`` returns within ``preempt_revert_timeout_s`` (the
  owner-activity poll loop that awaits it is never blocked longer), logs
  ``agent.bench.revert_timeout``, and the benchmark attempt still ends
  ``FAILED(owner_preempted)`` - its report says ``reverted = false``,
  ``revert_timeout``;
* the condition's **marker stays** (the start-up sweep's input) while the
  revert hangs; the shielded revert finishes later and removes it;
* one attempt whose stop raises does not keep the others running (0.4).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import structlog

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
from meshforge.common.proto_codec import failure_type_from_proto

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]


async def _wait_for(predicate: Any, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_hung_revert_is_bounded_the_attempt_stops_and_the_marker_stays(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    executor.preempt_revert_timeout_s = 0.5
    await executor.start()
    bench = _bench(_start(digest, "cpu_load:1"))
    attempt_id = bench.start_attempt.attempt_id
    executor.handle(bench)
    await asyncio.wait_for(runtime.running.wait(), 30)
    assert len(_burners()) == 1
    (condition,) = executor.conditions.active
    controller = condition.controller
    real_revert = controller.revert
    gate = asyncio.Event()

    async def hung_revert() -> bool:
        await gate.wait()
        return bool(await real_revert())

    controller.revert = hung_revert
    try:
        started = time.monotonic()
        with structlog.testing.capture_logs() as logs:
            stopped = await asyncio.wait_for(executor.preempt_benchmarks("paused"), 10)
            await _wait_for(lambda: _state(_final(reports, attempt_id)) == "failed")
        elapsed = time.monotonic() - started
        assert stopped == 1
        assert elapsed < 5.0, elapsed  # two 0.5 s bounds, never the hung revert
        timeouts = [e for e in logs if e["event"] == "agent.bench.revert_timeout"]
        assert timeouts and timeouts[0]["timeout_s"] == 0.5
        final = _final(reports, attempt_id)
        assert (
            failure_type_from_proto(final.failure.failure_type).value
            == "owner_preempted"
        )
        assert final.condition.reverted is False
        assert final.condition.reason == "revert_timeout"
        # still applied: the marker keeps it for the sweep, the burner runs
        assert attempt_id in executor.conditions.marker.load()
        assert len(_burners()) == 1
    finally:
        gate.set()  # the revert "returns": it finishes in the background
    await _wait_for(lambda: attempt_id not in executor.conditions.marker.load())
    await _no_burners()
    await _settle(executor)
    await executor.close()


@pytest.mark.asyncio
async def test_one_attempt_failing_to_stop_never_skips_the_others(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    msgs = [_bench(_start(digest, None)) for _ in range(3)]
    for m in msgs:
        executor.handle(m)
    await _wait_for(lambda: runtime.calls == 3)
    bad = msgs[0].start_attempt.attempt_id
    real_stop = executor._stop

    def flaky_stop(attempt: Any, reason: str) -> None:
        if attempt.attempt_id == bad:
            raise RuntimeError("stop failed")
        real_stop(attempt, reason)

    executor._stop = flaky_stop  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as logs:
        assert await executor.preempt_benchmarks("paused") == 3
    assert any(
        e["event"] == "agent.execution.preempt_stop_failed" and e["attempt_id"] == bad
        for e in logs
    )
    for m in msgs[1:]:
        aid = m.start_attempt.attempt_id
        await _wait_for(lambda aid=aid: _state(_final(reports, aid)) == "failed")
        assert executor._attempts[aid].preempt_stopped
    # the failed one is left for the guard's retry (marked, not stopped)
    assert not executor._attempts[bad].preempt_stopped
    executor._stop = real_stop  # type: ignore[method-assign]
    assert await executor.preempt_benchmarks("paused") == 1  # the retry gets it
    await _wait_for(lambda: _state(_final(reports, bad)) == "failed")
    await _settle(executor)
    await executor.close()
