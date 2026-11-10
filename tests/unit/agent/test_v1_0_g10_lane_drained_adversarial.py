"""V1.0 G10-B tester (D446): adversarial NoWork(drained | stop) timing.

Drain/stop arriving with a chunk grant, during a chunk's input staging, during
its result send, after results were already sent, twice, and drain-then-stop.
Built on the host of ``test_v1_0_g10_lane_drained_now``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_v1_0_g10_lane_drained_now import (
    _attempt,
    _grant,
    _HangingRuntime,
    _Host,
    _no_work,
    _output,
    _plan,
)

from meshforge.agent.execution.runtime import RunObserver, RunResult, RunSpec
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

DRAINED = agent_pb2.NoWork.REASON_DRAINED
STOP = agent_pb2.NoWork.REASON_STOP


class _OkThenHang(_HangingRuntime):
    """Chunk 1 succeeds at once; the next ones hang (lost copies)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def run(
        self, spec: RunSpec, on_output: Any, *, observer: RunObserver
    ) -> RunResult:
        self.calls += 1
        if self.calls == 1:
            observer.enter_env()
            observer.enter_run()
            return RunResult(exit_code=0)
        return await super().run(spec, on_output, observer=observer)


class _H(_Host):
    """Grants chunk 0, then chunk 1 (so a drain can land after a sent result)."""

    def __init__(self, tmp_path: Path, attempt: Any, *, grants: int = 1) -> None:
        super().__init__(tmp_path, attempt)
        self.grants = grants
        self.upload_gate: asyncio.Event | None = None
        self.in_upload = asyncio.Event()

    async def _send_now(self, name: str, payload: Any) -> bool:
        del payload
        self.requests += 1
        if self.requests <= self.grants:
            self._lane_command(_grant(self.requests - 1))
        return True

    async def _pack_upload(self, attempt: Any, plan: Any, folder: Any, bundle: Any):
        del attempt, plan, folder, bundle
        self.in_upload.set()
        if self.upload_gate is not None:
            await self.upload_gate.wait()
        return "sha256:" + "0" * 64, 1

    async def _send_chunk_result(self, attempt: Any, result: Any) -> None:
        await super()._send_chunk_result(attempt, result)


def _start(host: _Host, attempt: Any, runtime: Any) -> asyncio.Task[None]:
    return asyncio.ensure_future(
        host._run_lane(attempt, _plan(attempt.workdir), None, runtime, None, _output)  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_a_drain_arriving_in_the_same_tick_as_the_grant_ends_the_lane(
    tmp_path: Path,
) -> None:
    """Race: grant + NoWork(drained) queued back to back (one controller batch)
    before the lane task resumes. The hanging copy must not run on."""
    attempt = _attempt(tmp_path)
    host = _H(tmp_path, attempt, grants=0)
    runtime = _HangingRuntime()
    task = _start(host, attempt, runtime)
    await asyncio.sleep(0)  # the lane is now waiting in _ask
    await asyncio.sleep(0)
    host._lane_command(_grant(0))
    host._lane_command(_no_work(DRAINED))  # before the lane resumed
    try:
        await asyncio.wait_for(task, 3)
    except TimeoutError:
        task.cancel()
        pytest.fail("lane kept running a copy after NoWork(drained) arrived")
    assert host.chunk_results == []
    assert host.sent and host.sent[-1][0].name == "DONE"


@pytest.mark.asyncio
async def test_a_drain_during_chunk_input_staging_skips_the_chunk(
    tmp_path: Path,
) -> None:
    attempt = _attempt(tmp_path)
    host = _H(tmp_path, attempt)
    runtime = _HangingRuntime()
    gate = asyncio.Event()
    orig = host._chunk_inputs

    async def slow_inputs(*a: Any, **k: Any) -> Any:
        placed = await orig(*a, **k)
        await gate.wait()
        return placed

    host._chunk_inputs = slow_inputs  # type: ignore[method-assign]
    task = _start(host, attempt, runtime)
    for _ in range(50):
        if attempt.lane.current is not None:
            break
        await asyncio.sleep(0.01)
    assert attempt.lane.current is not None
    host._lane_command(_no_work(DRAINED))
    gate.set()
    await asyncio.wait_for(task, 3)
    assert not runtime.started.is_set()  # the copy never started
    assert host.chunk_results == []
    assert host.sent[-1][0].name == "DONE"


@pytest.mark.asyncio
async def test_results_already_sent_are_kept_when_a_later_copy_is_drained(
    tmp_path: Path,
) -> None:
    attempt = _attempt(tmp_path)
    host = _H(tmp_path, attempt, grants=2)
    runtime = _OkThenHang()
    task = _start(host, attempt, runtime)
    await asyncio.wait_for(runtime.started.wait(), 5)
    assert len(host.chunk_results) == 1  # chunk 0 already sent
    host._lane_command(_no_work(DRAINED))
    await asyncio.wait_for(task, 3)
    assert runtime.cancelled
    assert len(host.chunk_results) == 1  # kept, nothing added or dropped
    assert host.chunk_results[0].outcome == agent_pb2.ChunkResult.OUTCOME_DONE
    assert host.sent[-1][0].name == "DONE"


@pytest.mark.asyncio
async def test_a_drain_while_the_result_uploads_does_not_drop_the_result(
    tmp_path: Path,
) -> None:
    attempt = _attempt(tmp_path)
    host = _H(tmp_path, attempt)
    host.upload_gate = asyncio.Event()
    runtime = _OkThenHang()
    task = _start(host, attempt, runtime)
    await asyncio.wait_for(host.in_upload.wait(), 5)
    host._lane_command(_no_work(DRAINED))  # the chunk already ran
    host.upload_gate.set()
    await asyncio.wait_for(task, 3)
    assert len(host.chunk_results) == 1
    assert host.chunk_results[0].outcome == agent_pb2.ChunkResult.OUTCOME_DONE
    assert host.sent[-1][0].name == "DONE"


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [DRAINED, STOP])
async def test_a_duplicate_or_second_no_work_is_harmless(
    tmp_path: Path, second: int
) -> None:
    attempt = _attempt(tmp_path)
    host = _H(tmp_path, attempt)
    runtime = _HangingRuntime()
    task = _start(host, attempt, runtime)
    await asyncio.wait_for(runtime.started.wait(), 5)
    host._lane_command(_no_work(DRAINED))
    host._lane_command(_no_work(second))
    await asyncio.wait_for(task, 3)  # drained wins (read first): DONE
    assert host.sent[-1][0].name == "DONE"
    assert runtime.cancelled
    assert not attempt.lane.held


@pytest.mark.asyncio
async def test_stop_then_drain_ends_cancelled_not_done(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _H(tmp_path, attempt)
    runtime = _HangingRuntime()
    task = _start(host, attempt, runtime)
    await asyncio.wait_for(runtime.started.wait(), 5)
    host._lane_command(_no_work(STOP))
    host._lane_command(_no_work(DRAINED))
    with pytest.raises(RuntimeError, match="stopped"):
        await asyncio.wait_for(task, 3)
    assert host.sent == []
