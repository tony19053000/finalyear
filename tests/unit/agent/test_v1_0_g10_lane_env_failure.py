"""V1.0 G10 BUG-2 (D443, lab L1 G9-B c, job_01M72QTRYT43884YFENTMYY7N9): a
failed env build was retried for **every chunk** of a lane.

Each chunk of a lane is one ``runtime.run``, and the runtime builds the env
when it is not cached - so a build that fails ran again for the next chunk:
8 chunks = 8 torch downloads (~4.4 GB), each reported as a *chunk* failure
(retryable ``env_error``), the lane kept pulling, and the job never ended.

Now a chunk whose run failed **before its workload started** (in the env
phase: image, env build, setup) ends the lane: no ``ChunkResult`` (the lease
goes back to the queue uncounted when the controller records the lane's end -
the chunk did nothing wrong), no further ``ChunkRequest``, and the lane's
attempt ends ``FAILED`` with the build's own typed failure (``env_error`` is
not retryable - the controller marks ``(node, env)`` known-bad, D116/D433, and
the job finishes on its other lanes or fails cleanly). A chunk whose workload
ran and failed keeps the D267c path (a chunk result; the lane goes on).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.execution.lane import LaneSteps
from meshforge.agent.execution.lane_base import LaneEnvFailedError, LaneState
from meshforge.agent.execution.runtime import (
    RunLimits,
    RunObserver,
    RunResult,
    RunSpec,
)
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.split_chunks import ChunkSpan, ItemList, chunk_items_digest
from meshforge.common.states import AttemptState

pytestmark = pytest.mark.unit

TOKEN = 7
ATTEMPT_ID = str(new_attempt_id())
ITEMS = ItemList(over="range", items=tuple(range(8)))
ENV_HASH = "e" * 64


def _grant(index: int) -> agent_pb2.ControllerMessage:
    span = ChunkSpan(index * 2, 2)
    return agent_pb2.ControllerMessage(
        chunk_grant=agent_pb2.ChunkGrant(
            chunks=[
                agent_pb2.GrantedChunk(
                    chunk_index=index,
                    item_id=100 + index,
                    lease_seq=1,
                    first_item=span.first,
                    item_count=span.count,
                    items_digest=str(chunk_items_digest(ITEMS.chunk(span))),
                )
            ]
        )
    )


DRAINED = agent_pb2.ControllerMessage(
    no_work=agent_pb2.NoWork(reason=agent_pb2.NoWork.REASON_DRAINED)
)


class _Runtime:
    """``run`` = an env-phase failure (never started the workload) or a
    workload that ran and failed, per ``mode``."""

    def __init__(self, mode: str, failure: FailureType = FailureType.ENV_ERROR):
        self.mode = mode
        self.failure = failure
        self.calls = 0

    async def run(
        self, spec: RunSpec, on_output: Any, *, observer: RunObserver
    ) -> RunResult:
        del spec, on_output
        self.calls += 1
        observer.enter_env()
        if self.mode == "blip":  # D449: Docker daemon unreachable for a moment
            observer.finish()
            return RunResult(
                exit_code=None,
                failure_type=FailureType.RESOURCE_BUSY,
                reason="docker_unavailable",
                detail="cannot reach the Docker daemon: ConnectionError",
            )
        if self.mode == "env":
            observer.finish()
            return RunResult(
                exit_code=1,
                failure_type=self.failure,
                reason="setup_failed"
                if self.failure is FailureType.ENV_ERROR
                else "env_timeout",
                detail="requirements install failed (exit 1)",
                env_s=3.0,
                env_hash=ENV_HASH,
            )
        observer.enter_run()
        observer.finish()
        return RunResult(
            exit_code=1,
            failure_type=FailureType.USER_ERROR,
            detail="exit 1",
            env_hash=ENV_HASH,
        )


class _Host(LaneSteps):
    def __init__(self, tmp_path: Path, replies: list[agent_pb2.ControllerMessage]):
        self._node_id = "node-1"
        self._clock = asyncio.get_event_loop().time
        self._cfg = SimpleNamespace(
            max_written_bytes=1 << 30,
            output_check_interval_s=60.0,
            free_disk_read_failure_limit=5,
            free_disk_read_timeout_s=0.5,
        )
        self.cas = SimpleNamespace(store=SimpleNamespace(free_disk_floor_bytes=0))
        self.work_root = tmp_path
        self._send_now = None
        self._replies = replies
        self.asked = 0
        self.chunk_results: list[agent_pb2.ChunkResult] = []
        self.sent: list[tuple[AttemptState, Any]] = []

    async def _step(self, attempt: Any, awaitable: Any) -> Any:
        del attempt
        return await awaitable

    async def _announce_run(self, attempt: Any) -> None:
        del attempt

    async def _ask(self, attempt: Any) -> agent_pb2.ControllerMessage:
        del attempt
        self.asked += 1
        return self._replies.pop(0) if self._replies else DRAINED

    async def _chunk_inputs(self, attempt: Any, plan: Any, folder: Any, *_: Any) -> Any:
        del attempt, plan
        folder.out.mkdir(parents=True)
        return []

    async def _send_chunk_result(
        self, attempt: Any, result: agent_pb2.ChunkResult
    ) -> None:
        del attempt
        self.chunk_results.append(result)

    async def _final_logs(self, attempt: Any, plan: Any, logs: Any) -> Any:
        del attempt, plan, logs
        return {}

    async def _send(self, attempt: Any, state: AttemptState, **kw: Any) -> None:
        del attempt
        self.sent.append((state, kw))

    def _stopped_error(self) -> Exception:
        return RuntimeError("stopped")


def _attempt(tmp_path: Path) -> Any:
    lane = LaneState(spec=agent_pb2.LaneSpec(lease_timeout_s=60))
    lane.items = ITEMS
    workdir = tmp_path / ATTEMPT_ID
    workdir.mkdir()
    return SimpleNamespace(
        attempt_id=ATTEMPT_ID,
        job_id="job-1",
        token=TOKEN,
        ended=False,
        lane=lane,
        workdir=workdir,
        stop_reason=None,
        step=None,
        env_hash=None,
        phases={},
    )


def _plan(workdir: Path) -> Any:
    spec = RunSpec(
        attempt_id=ATTEMPT_ID,
        argv=("python", "main.py"),
        workdir=workdir,
        env={},
        limits=RunLimits(memory_bytes=64 << 20, pids=64, timeout_s=10),
    )
    return SimpleNamespace(runtime="docker", spec=spec)


async def _output(stream: Any, data: bytes) -> None:
    del stream, data


async def _run(host: _Host, runtime: _Runtime, attempt: Any) -> None:
    plan = _plan(attempt.workdir)
    await host._run_lane(attempt, plan, None, runtime, None, _output)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failed_env_build_ends_the_lane_after_one_build(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path, [_grant(0), _grant(1), _grant(2), _grant(3)])
    runtime = _Runtime("env")
    attempt = _attempt(tmp_path)
    await _run(host, runtime, attempt)
    assert runtime.calls == 1  # one build, never one per chunk
    assert host.asked == 1  # no ChunkRequest after the failed build
    assert host.chunk_results == []  # the lease is released, not failed
    ((state, kw),) = host.sent
    assert state is AttemptState.FAILED
    kind, text, retryable = kw["failure"]
    assert kind is FailureType.ENV_ERROR
    assert retryable is False  # the controller marks (node, env) known-bad
    assert "setup_failed" in text and "env" in text
    assert attempt.env_hash == ENV_HASH  # reported with the lane's end
    assert attempt.phases["env_s"] == pytest.approx(3.0)
    assert not attempt.lane.held  # nothing held on the agent any more
    gone = await asyncio.to_thread(lambda: not list(tmp_path.glob("*.chunk")))
    assert gone  # the chunk's folder is removed after it


@pytest.mark.asyncio
async def test_an_env_timeout_ends_the_lane_retryable(tmp_path: Path) -> None:
    host = _Host(tmp_path, [_grant(0), _grant(1)])
    runtime = _Runtime("env", FailureType.TIMEOUT)
    attempt = _attempt(tmp_path)
    await _run(host, runtime, attempt)
    assert runtime.calls == 1
    ((state, kw),) = host.sent
    assert state is AttemptState.FAILED
    assert kw["failure"][0] is FailureType.TIMEOUT
    assert kw["failure"][2] is True  # Mode 1's rule for a timeout


@pytest.mark.asyncio
async def test_a_workload_failure_keeps_the_chunk_path(tmp_path: Path) -> None:
    host = _Host(tmp_path, [_grant(0), _grant(1)])
    runtime = _Runtime("run")
    attempt = _attempt(tmp_path)
    await _run(host, runtime, attempt)
    assert runtime.calls == 2  # each chunk ran; the lane went on to drained
    assert [r.outcome for r in host.chunk_results] == [
        agent_pb2.ChunkResult.OUTCOME_FAILED
    ] * 2
    ((state, _kw),) = host.sent
    assert state is AttemptState.DONE


def test_the_error_carries_a_typed_failure() -> None:
    exc = LaneEnvFailedError(FailureType.ENV_ERROR, "setup_failed: boom", False)
    assert (exc.failure_type, exc.message, exc.retryable) == (
        FailureType.ENV_ERROR,
        "setup_failed: boom",
        False,
    )


@pytest.mark.asyncio
async def test_a_docker_daemon_blip_does_not_end_the_lane(tmp_path: Path) -> None:
    """D449: ``docker_unavailable`` (raised in the env phase, before any build)
    is retryable ``resource_busy``: the chunk fails retryable and the lane goes
    on - no lane end, so no known-bad env on the controller."""
    host = _Host(tmp_path, [_grant(0), _grant(1)])
    runtime = _Runtime("blip")
    attempt = _attempt(tmp_path)
    await _run(host, runtime, attempt)
    assert runtime.calls == 2  # every chunk tried
    assert len(host.chunk_results) == 2
    for result in host.chunk_results:
        assert result.outcome == agent_pb2.ChunkResult.OUTCOME_FAILED
        assert result.failure.retryable is True
    ((state, _kw),) = host.sent
    assert state is AttemptState.DONE
