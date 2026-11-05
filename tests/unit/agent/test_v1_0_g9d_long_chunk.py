"""V1.0 G9-D (D437, lab finding L1 C10.10-c): "a long single chunk loses its
lease forever".

Root cause (not the lease): the chunk ran under the job's own time limit
(``limits.timeout``, 15 min for the lab's benchmark folder), was killed at it
(``deadline_exceeded``) and was reported **retryable**, so the controller
leased the same, only chunk to the same, only lane again - restarting from zero
each time until ``retry.max_attempts`` ran out. A chunk over the job's own
time limit is now final (as a whole job's ``deadline_exceeded`` is).

The lease itself is guarded too: the renew loop keeps a chunk's lease alive for
hours (no cap), and one failed renewal write does not end renewals for the rest
of the lane.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.execution import lane as lane_module
from meshforge.agent.execution.lane import LaneSteps
from meshforge.agent.execution.lane_base import CHUNK_FAILURE_NOT_RETRYABLE, LaneState
from meshforge.agent.execution.lane_chunk import ChunkRunSteps, _run_failure
from meshforge.agent.execution.runtime import RunResult
from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

LEASE_TIMEOUT_S = 60.0
TWO_HOURS_TICKS = int(2 * 3600 / (LEASE_TIMEOUT_S / 3))  # one renewal per 20 s


# ------------------------------------------------------------ the root cause


def test_a_chunk_over_the_jobs_time_limit_is_not_retried() -> None:
    run = RunResult(
        exit_code=None,
        signal=9,
        timed_out=True,
        failure_type=FailureType.DEADLINE_EXCEEDED,
        detail="time limit of 900 s exceeded; stopped",
    )
    assert _run_failure(run, None) == (FailureType.DEADLINE_EXCEEDED, False)
    assert FailureType.DEADLINE_EXCEEDED in CHUNK_FAILURE_NOT_RETRYABLE


@pytest.mark.parametrize(
    "kind",
    [FailureType.USER_ERROR, FailureType.OOM, FailureType.TIMEOUT],
)
def test_other_workload_failures_stay_retryable_elsewhere(kind: FailureType) -> None:
    """D267c unchanged for the rest: another node may succeed."""
    assert _run_failure(RunResult(exit_code=1, failure_type=kind), None) == (kind, True)


def test_the_failure_text_names_the_per_chunk_time_limit() -> None:
    run = RunResult(
        exit_code=None,
        timed_out=True,
        failure_type=FailureType.DEADLINE_EXCEEDED,
        detail="time limit of 900 s exceeded; stopped",
    )
    lane = LaneState(spec=agent_pb2.LaneSpec())
    text = ChunkRunSteps._run_text(SimpleNamespace(), run, lane)  # type: ignore[arg-type]
    assert text.startswith("time limit of 900 s exceeded; stopped")
    assert "limits.timeout bounds each chunk" in text
    other = ChunkRunSteps._run_text(  # type: ignore[arg-type]
        SimpleNamespace(), RunResult(exit_code=1, detail="boom"), lane
    )
    assert other == "boom"


# ------------------------------------------------------------ the renew loop


class _Host(LaneSteps):
    def __init__(self, send_now: Any) -> None:
        self._node_id = "node-1"
        self._attempts = {}
        self._send_now = send_now


def _attempt() -> Any:
    lane = LaneState(spec=agent_pb2.LaneSpec(lease_timeout_s=LEASE_TIMEOUT_S))
    lane.held[(41, 1)] = 0  # one long chunk, granted once, never finishing
    return SimpleNamespace(
        attempt_id="att-1", job_id="job-1", token=7, lane=lane, stop_reason=None
    )


class _FakeTime:
    """``asyncio.sleep`` that advances a fake clock and yields at once."""

    def __init__(self) -> None:
        self.now = 0.0
        self._real_sleep = asyncio.sleep

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await self._real_sleep(0)


async def _renew_for(
    monkeypatch: pytest.MonkeyPatch,
    ticks: int,
    *,
    fail_first: int = 0,
    error: type[BaseException] = RuntimeError,
) -> tuple[list[tuple[float, agent_pb2.LeaseRenew]], _FakeTime]:
    fake = _FakeTime()
    monkeypatch.setattr(
        lane_module, "asyncio", SimpleNamespace(sleep=fake.sleep), raising=True
    )
    sent: list[tuple[float, agent_pb2.LeaseRenew]] = []
    calls = 0
    enough = asyncio.Event()

    async def send_now(name: str, payload: Any) -> bool:
        nonlocal calls
        assert name == "lease_renew"
        calls += 1
        if calls <= fail_first:
            raise error("the stream broke mid-write")
        sent.append((fake.now, payload))
        if len(sent) >= ticks:
            enough.set()
        return True

    host = _Host(send_now)
    task = asyncio.ensure_future(host._renew_loop(_attempt()))
    try:
        await asyncio.wait_for(enough.wait(), 10.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return sent, fake


@pytest.mark.asyncio
async def test_renewals_keep_a_two_hour_chunk_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, fake = await _renew_for(monkeypatch, TWO_HOURS_TICKS)
    assert len(sent) == TWO_HOURS_TICKS
    assert fake.now >= 2 * 3600
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(sent)]
    assert max(gaps) <= LEASE_TIMEOUT_S / 3  # always well inside the timeout
    for _at, renew in sent:
        assert [(r.item_id, r.lease_seq) for r in renew.leases] == [(41, 1)]
        assert (renew.job_id, renew.attempt_id, renew.fencing_token) == (
            "job-1",
            "att-1",
            7,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, OSError, ConnectionResetError])
async def test_a_failed_renewal_write_does_not_end_renewals(
    monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    sent, _fake = await _renew_for(monkeypatch, 5, fail_first=2, error=error)
    assert len(sent) == 5
