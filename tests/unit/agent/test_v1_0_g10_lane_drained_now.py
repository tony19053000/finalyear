"""V1.0 G10 BUG-3 (D446, lab L1 G9-B c run 2 and V0.10 C c4): a lane showed
``running exit=None`` for minutes after its job succeeded.

When another lane commits the job's last chunk, the controller sends the other
lanes ``NoWork(drained)`` unasked (``split/service.py`` ``_drained``). The lane
read it only at its next ``ChunkRequest`` - after it finished staging, built
its env, or ran a speculative copy that had already lost. Now the ``NoWork``
acts when it arrives:

* in the pull loop: the running chunk (a lost copy, an env build) is killed
  with no result, the loop reads the ``NoWork`` from its inbox **without a new
  ChunkRequest** and the lane ends ``DONE`` (``stop``: ``cancelled``);
* still staging: the staging is stopped (``Executor._stop``) and the lane ends
  ``DONE`` (``drained``) / ``cancelled`` (``stop``);
* ``NoWork(wait)`` never arrives unasked and changes nothing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.lane import LaneSteps
from meshforge.agent.execution.lane_base import LANE_DRAINED, LaneState
from meshforge.agent.execution.runtime import (
    RunLimits,
    RunObserver,
    RunResult,
    RunSpec,
)
from meshforge.common.ids import new_attempt_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.split_chunks import ChunkSpan, ItemList, chunk_items_digest
from meshforge.common.states import AttemptState

pytestmark = pytest.mark.unit

TOKEN = 7
ATTEMPT_ID = str(new_attempt_id())
ITEMS = ItemList(over="range", items=tuple(range(8)))


def _grant(index: int) -> agent_pb2.ControllerMessage:
    span = ChunkSpan(index * 2, 2)
    return agent_pb2.ControllerMessage(
        chunk_grant=agent_pb2.ChunkGrant(
            job_id="job-1",
            attempt_id=ATTEMPT_ID,
            fencing_token=TOKEN,
            chunks=[
                agent_pb2.GrantedChunk(
                    chunk_index=index,
                    item_id=100 + index,
                    lease_seq=2,
                    first_item=span.first,
                    item_count=span.count,
                    items_digest=str(chunk_items_digest(ITEMS.chunk(span))),
                    speculative=True,
                )
            ],
        )
    )


def _no_work(reason: int) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        no_work=agent_pb2.NoWork(
            job_id="job-1", attempt_id=ATTEMPT_ID, fencing_token=TOKEN, reason=reason
        )
    )


class _HangingRuntime:
    """A chunk that never ends by itself (a lost copy / a long env build)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(
        self, spec: RunSpec, on_output: Any, *, observer: RunObserver
    ) -> RunResult:
        del spec, on_output
        observer.enter_env()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class _Host(LaneSteps):
    def __init__(self, tmp_path: Path, attempt: Any) -> None:
        self._node_id = "node-1"
        self._clock = time.monotonic
        self._cfg = SimpleNamespace(
            max_written_bytes=1 << 30,
            output_check_interval_s=60.0,
            free_disk_read_failure_limit=5,
            free_disk_read_timeout_s=0.5,
        )
        self.cas = SimpleNamespace(store=SimpleNamespace(free_disk_floor_bytes=0))
        self.work_root = tmp_path
        self._attempts = {attempt.attempt_id: attempt}
        self.requests = 0
        self.chunk_results: list[agent_pb2.ChunkResult] = []
        self.sent: list[tuple[AttemptState, Any]] = []
        self.stops: list[str] = []

    async def _send_now(self, name: str, payload: Any) -> bool:
        assert name == "chunk_request", name
        del payload
        self.requests += 1
        if self.requests == 1:
            self._lane_command(_grant(0))
        return True

    async def _step(self, attempt: Any, awaitable: Any) -> Any:
        del attempt
        return await awaitable

    async def _announce_run(self, attempt: Any) -> None:
        del attempt

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

    def _stop(self, attempt: Any, reason: str) -> None:
        del attempt
        self.stops.append(reason)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "ends"),
    [
        (agent_pb2.NoWork.REASON_DRAINED, AttemptState.DONE),
        (agent_pb2.NoWork.REASON_STOP, None),  # stopped -> the executor's cancel
    ],
)
async def test_an_unasked_no_work_kills_the_running_chunk_and_ends_the_lane(
    tmp_path: Path, reason: int, ends: AttemptState | None
) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(tmp_path, attempt)
    runtime = _HangingRuntime()
    plan = _plan(attempt.workdir)
    lane_task = asyncio.ensure_future(
        host._run_lane(attempt, plan, None, runtime, None, _output)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(runtime.started.wait(), 5)
    host._lane_command(_no_work(reason))  # arrives while the chunk runs
    if ends is AttemptState.DONE:
        await asyncio.wait_for(lane_task, 5)
        ((state, kw),) = host.sent
        assert state is AttemptState.DONE and kw["exit_code"] == 0
    else:
        with pytest.raises(RuntimeError, match="stopped"):
            await asyncio.wait_for(lane_task, 5)
        assert host.sent == []  # the executor reports the stop (cancelled)
    assert runtime.cancelled  # the lost copy / env build was killed
    assert host.chunk_results == []  # no result for a killed chunk
    assert host.requests == 1  # the NoWork was read without a new request
    assert host.stops == []  # in the pull loop: only the chunk is stopped
    assert not attempt.lane.held


@pytest.mark.parametrize(
    ("reason", "stop"),
    [
        (agent_pb2.NoWork.REASON_DRAINED, LANE_DRAINED),
        (agent_pb2.NoWork.REASON_STOP, "cancelled"),
    ],
)
def test_an_unasked_no_work_while_staging_stops_the_staging(
    tmp_path: Path, reason: int, stop: str
) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(tmp_path, attempt)
    assert attempt.lane.pulling is False  # still staging
    host._lane_command(_no_work(reason))
    assert host.stops == [stop]


def test_a_wait_never_stops_anything(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(tmp_path, attempt)
    host._lane_command(_no_work(agent_pb2.NoWork.REASON_WAIT))
    assert host.stops == []
    attempt.lane.pulling = True
    attempt.lane.current = (100, 2)
    host._lane_command(_no_work(agent_pb2.NoWork.REASON_WAIT))
    assert attempt.lane.cancelled_chunk is None


def test_a_no_work_for_another_token_is_ignored(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(tmp_path, attempt)
    msg = _no_work(agent_pb2.NoWork.REASON_DRAINED)
    msg.no_work.fencing_token = TOKEN + 1
    host._lane_command(msg)
    assert host.stops == []
    assert attempt.lane.inbox.empty()


@pytest.mark.asyncio
async def test_a_lane_drained_while_staging_reports_done() -> None:
    """``Executor._stopped`` for :data:`LANE_DRAINED`: ``DONE``, exit 0."""
    sent: list[tuple[AttemptState, Any]] = []

    async def send(attempt: Any, state: AttemptState, **kw: Any) -> None:
        del attempt
        sent.append((state, kw))

    fake = SimpleNamespace(_send=send)
    attempt = SimpleNamespace(stop_reason=LANE_DRAINED)
    await Executor._stopped(fake, attempt, exit_code=None, signal=None)  # type: ignore[arg-type]
    assert sent == [(AttemptState.DONE, {"exit_code": 0})]
