"""V1.0 G10 BUG-4 (D451), controller half: a lease its lane reports stalled
(``ChunkLeaseRef.stalled_s`` on ``LeaseRenew``) is copied first - over a real
controller (PostgreSQL, gRPC sessions, HTTPS blobs) with fake lanes.

Lab L4 chunk 68: the worker hung 17 min; the lane kept renewing; the chunk was
copied only when the queue ran empty (and only with a time estimate). Now:

* two nodes, chunks still pending: the other lane gets a copy of the stalled
  chunk on its next ask - before the pending ones - the copy wins (D266), the
  hung original gets ``CancelChunk``, the job ends long before the original
  would have; the original's chunk record carries its ``stalled_s``;
* one node: no copy is possible on another node, so the lease is kept (still
  renewed: a reclaim would only run it again there) and the controller logs
  ``controller.split.chunk_stalled`` ``action=alone``; when the chunk finally
  finishes, its committed record carries ``stalled_s``;
* a renewal without ``stalled_s`` clears the mark (the chunk resumed).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from mf_fake_lanes import FakeLane

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from test_v10_g6_speculation import (
    _expected_output,
    _lanes,
    _leases,
    _sql,
    _stats,
    _submit,
    _until,
    _wait_job,
    http,
    runtime,
)

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

__all__ = ["http", "runtime"]  # fixtures, re-used

HUNG_S = 60.0


def _hang_first() -> tuple[list[int], Any]:
    """A lane whose FIRST chunk hangs (whichever index it is)."""
    hung: list[int] = []

    def speed(grant: agent_pb2.GrantedChunk) -> float:
        if not hung:
            hung.append(int(grant.chunk_index))
        return HUNG_S if grant.chunk_index == hung[0] else 0.3

    return hung, speed


def _lane_lease(lane: FakeLane, chunk_index: int) -> tuple[Any, Any]:
    """The lane's StartAttempt and its grant of ``chunk_index``."""
    start = next(
        m.start_attempt
        for m in lane.agent.received
        if m.WhichOneof("payload") == "start_attempt"
        and m.start_attempt.HasField("lane")
    )
    granted = next(
        c
        for m in lane.agent.received
        if m.WhichOneof("payload") == "chunk_grant"
        for c in m.chunk_grant.chunks
        if c.chunk_index == chunk_index and not c.speculative
    )
    return start, granted


async def _renew(lane: FakeLane, start: Any, granted: Any, stalled_s: float) -> None:
    await lane._write(
        lease_renew=agent_pb2.LeaseRenew(
            job_id=start.job_id,
            attempt_id=start.attempt_id,
            fencing_token=start.fencing_token,
            leases=[
                agent_pb2.ChunkLeaseRef(
                    item_id=granted.item_id,
                    lease_seq=granted.lease_seq,
                    stalled_s=stalled_s,
                )
            ],
        )
    )


async def _record(runtime: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        runtime,
        "SELECT chunk_index, lease_seq, node_id, speculative, won, outcome, "
        "stalled_s FROM chunk_record WHERE job_id = :job "
        "ORDER BY chunk_index, lease_seq",
        job=job_id,
    )


async def test_a_stalled_chunk_is_copied_first_while_chunks_are_pending(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    first, speed = _hang_first()
    async with _lanes(runtime, [{"chunk_speed": speed}, {"speed_s": 0.3}]) as lanes:
        hung, other = lanes
        job_id = await _submit(runtime, http, tmp_path, items=40, chunk=2)
        loop = asyncio.get_running_loop()
        await _until(lambda: first)
        idx = first[0]
        start, granted = _lane_lease(hung, idx)
        marked_at = loop.time()
        with structlog.testing.capture_logs() as logs:
            await _renew(hung, start, granted, 120.0)
            job, ended_at = await _wait_job(runtime, job_id, within=HUNG_S)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        assert ended_at - marked_at < HUNG_S / 2  # not waiting for the hung chunk
        # the other lane copied chunk 0, its copy won, the original was cancelled
        assert [c for c, _ in other.speculative] == [idx]
        # the hung original was told to stop (CancelChunk) and never committed
        await _until(
            lambda: any(
                m.WhichOneof("payload") == "cancel_chunk"
                and m.cancel_chunk.item_id == granted.item_id
                and m.cancel_chunk.lease_seq == granted.lease_seq
                for m in hung.agent.received
            )
        )
        assert not any(ev == "result" and c == idx for ev, _, c, _ in hung.timeline)
        stats = _stats(runtime)
        assert stats.stalls == 1 and stats.stalls_alone == 0
        assert stats.copies_won == 1
        stalled = [e for e in logs if e["event"] == "controller.split.chunk_stalled"]
        assert len(stalled) == 1 and stalled[0]["action"] == "copy_first"
        # copied before the queue ran empty: an ordinary lease began after it
        leases = await _leases(runtime, job_id)
        copy = next(r for r in leases if r.speculative)
        later = [
            r for r in leases if not r.speculative and r.leased_at > copy.leased_at
        ]
        assert later, "the copy waited for the tail"
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        from test_v10_g6_speculation import _blob

        assert await _blob(http, detail["output_digest"]) == _expected_output(40, 2)
        rows = {(r.chunk_index, r.lease_seq): r for r in await _record(runtime, job_id)}
        original = rows[(idx, int(granted.lease_seq))]
        assert original.outcome == "superseded" and not original.won
        assert original.stalled_s == pytest.approx(120.0)
        won = rows[(idx, int(copy.seq))]
        assert won.won and won.stalled_s is None


async def test_one_node_keeps_renewing_and_says_so(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(
        runtime, [{"chunk_speed": lambda g: 4.0 if g.chunk_index == 0 else 0.1}]
    ) as lanes:
        (alone,) = lanes
        job_id = await _submit(runtime, http, tmp_path, items=4, chunk=2)
        await _until(lambda: any(ev == "start" for ev, *_ in alone.timeline))
        start, granted = _lane_lease(alone, 0)
        with structlog.testing.capture_logs() as logs:
            await _renew(alone, start, granted, 90.0)
            await _until(lambda: _stats(runtime).stalls == 1)
        assert _stats(runtime).stalls_alone == 1
        (line,) = [e for e in logs if e["event"] == "controller.split.chunk_stalled"]
        assert line["action"] == "alone" and line["other_lanes"] == 0
        assert _stats(runtime).renewed >= 1  # still renewed, never reclaimed
        live = [r for r in await _leases(runtime, job_id) if r.state == "live"]
        assert [(r.idx, r.seq) for r in live] == [(0, int(granted.lease_seq))]
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        assert alone.speculative == []  # never a copy on its own node
        rows = await _record(runtime, job_id)
        first = next(r for r in rows if r.chunk_index == 0)
        assert first.outcome == "committed" and first.stalled_s == pytest.approx(90.0)


async def test_a_renewal_without_stalled_s_clears_the_mark(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(
        runtime, [{"chunk_speed": lambda g: 5.0 if g.chunk_index == 0 else 0.1}]
    ) as lanes:
        (lane,) = lanes
        await _submit(runtime, http, tmp_path, items=4, chunk=2)
        await _until(lambda: any(ev == "start" for ev, *_ in lane.timeline))
        start, granted = _lane_lease(lane, 0)
        assert runtime.dispatcher is not None
        split = runtime.dispatcher.split
        await _renew(lane, start, granted, 30.0)
        await _until(lambda: int(granted.item_id) in split._stalls)
        with structlog.testing.capture_logs() as logs:
            await _renew(lane, start, granted, 0.0)
            await _until(lambda: int(granted.item_id) not in split._stalls)
        assert [e for e in logs if e["event"] == "controller.split.chunk_resumed"]
