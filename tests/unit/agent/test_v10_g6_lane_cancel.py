"""V0.10 G6 (D266): a ``CancelChunk`` that reaches a lane before the chunk ran
there (the speculative copy - or the original - lost while its grant was still
on the way) skips the chunk: no process, no result. A cancel that arrives
while the chunk's inputs are staged stops it before its process starts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.execution.lane import LaneSteps
from meshforge.agent.execution.lane_base import MAX_EARLY_CANCELS, LaneState
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

TOKEN = 7


class _Host(LaneSteps):
    def __init__(self, attempt: Any) -> None:
        self._node_id = "node-1"
        self._attempts = {attempt.attempt_id: attempt}

    async def _send_chunk_result(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a skipped chunk sends no result")


def _attempt(tmp_path: Path) -> Any:
    lane = LaneState(spec=agent_pb2.LaneSpec())
    lane.items = SimpleNamespace(over="range")  # type: ignore[assignment]
    return SimpleNamespace(
        attempt_id="att-1",
        job_id="job-1",
        token=TOKEN,
        ended=False,
        lane=lane,
        workdir=tmp_path,
        stop_reason=None,
    )


def _cancel(item_id: int, seq: int, token: int = TOKEN) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        cancel_chunk=agent_pb2.CancelChunk(
            job_id="job-1",
            attempt_id="att-1",
            fencing_token=token,
            item_id=item_id,
            lease_seq=seq,
            reason="superseded",
        )
    )


def _grant(item_id: int, seq: int) -> agent_pb2.GrantedChunk:
    return agent_pb2.GrantedChunk(
        chunk_index=3, item_id=item_id, lease_seq=seq, first_item=0, item_count=1
    )


def test_an_early_cancel_skips_the_chunk(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    host._lane_command(_cancel(41, 2))
    assert (41, 2) in attempt.lane.cancelled_early
    asyncio.run(
        host._chunk(attempt, None, None, None, None, None, _grant(41, 2))  # type: ignore[arg-type]
    )
    assert (41, 2) not in attempt.lane.cancelled_early  # consumed once
    assert attempt.lane.current is None


def test_a_cancel_for_a_finished_lease_or_a_stale_token_is_not_kept(
    tmp_path: Path,
) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    attempt.lane.finished.add((5, 1))
    host._lane_command(_cancel(5, 1))
    host._lane_command(_cancel(6, 1, token=TOKEN + 1))  # fenced: another lane
    assert not attempt.lane.cancelled_early


def test_early_cancels_are_bounded(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    for i in range(MAX_EARLY_CANCELS + 10):
        host._lane_command(_cancel(i, 1))
    early = attempt.lane.cancelled_early
    assert len(early) == MAX_EARLY_CANCELS
    assert (0, 1) not in early and (MAX_EARLY_CANCELS + 9, 1) in early


def test_a_cancel_of_the_running_chunk_marks_it(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    attempt.lane.current = (9, 3)
    host._lane_command(_cancel(9, 3))
    assert attempt.lane.cancelled_chunk == (9, 3)
    assert not attempt.lane.cancelled_early


def test_cancelled_while_staging_never_starts_a_process(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    attempt.lane.current = attempt.lane.cancelled_chunk = (9, 3)
    got = asyncio.run(
        host._chunk_run(attempt, None, None, None, None, None, _grant(9, 3))  # type: ignore[arg-type]
    )
    assert got is None
