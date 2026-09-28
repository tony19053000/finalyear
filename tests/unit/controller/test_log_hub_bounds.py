"""V0.4 G5 fix round (reviewer N3): the LogHub writer queue is bounded.

Live chunks are admitted up to ``queue_chunks`` and attempt ends up to
``queue_finals`` (``MAX_PENDING_FINALS``); the queue itself is sized to their
sum, so ``put_nowait`` can never raise and nothing grows without limit. Except
for the last test no writer task runs, so the queue only fills.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.jobs.logs import MAX_PENDING_FINALS, LogEvent, LogHub

pytestmark = pytest.mark.unit

J, N = "job_1", "node_1"
LIVE = SimpleNamespace(node_id=N)


def _msg(attempt: str, offset: int) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        log_chunk=agent_pb2.LogChunk(
            attempt_id=attempt,
            job_id=J,
            stream=agent_pb2.LogChunk.STREAM_STDOUT,
            offset=offset,
            data=b"x",
            fencing_token=1,
        )
    )


def test_default_bound_is_the_documented_constant(tmp_path: Path) -> None:
    hub = LogHub(tmp_path, blobs=None, max_bytes=10, queue_chunks=16)
    assert hub._queue.maxsize == 16 + MAX_PENDING_FINALS


@pytest.mark.asyncio
async def test_chunks_max_accepted_max_plus_one_dropped(tmp_path: Path) -> None:
    hub = LogHub(tmp_path, blobs=None, max_bytes=10**6, queue_chunks=3, queue_finals=2)
    hub.open("att_a", job_id=J, node_id=N, token=1)
    for i in range(3):
        await hub.on_chunk(LIVE, _msg("att_a", i))  # type: ignore[arg-type]
    assert hub._queue.qsize() == 3 and hub.stats.chunks_dropped == 0
    await hub.on_chunk(LIVE, _msg("att_a", 3))  # type: ignore[arg-type]
    assert hub._queue.qsize() == 3 and hub.stats.chunks_dropped == 1


@pytest.mark.asyncio
async def test_finals_max_queued_max_plus_one_applied_at_once(tmp_path: Path) -> None:
    hub = LogHub(tmp_path, blobs=None, max_bytes=10**6, queue_chunks=3, queue_finals=2)
    for name in ("att_a", "att_b", "att_c"):
        hub.open(name, job_id=J, node_id=N, token=1)
    for i in range(3):  # chunks full too: finals still have their own room
        await hub.on_chunk(LIVE, _msg("att_a", i))  # type: ignore[arg-type]
    sub = hub.subscribe("att_c", J)
    assert sub is not None
    hub.finalize("att_a", None)
    hub.finalize("att_b", None)
    assert hub._queue.qsize() == 5 and hub.stats.finals_overflowed == 0
    assert hub.ended_state("att_a") is None  # queued, not applied yet
    hub.finalize("att_c", {"stdout": {"digest": "sha256:" + "0" * 64}})
    assert hub._queue.qsize() == 5  # max + 1: not queued
    assert hub.stats.finals_overflowed == 1
    ended = hub.ended_state("att_c")
    assert ended is not None and ended.ended and ended.final == {}
    assert hub.live_state("att_c") is None
    assert sub.queue.get_nowait() == LogEvent("end")  # followers still end


@pytest.mark.asyncio
async def test_applied_finals_free_their_slot(tmp_path: Path) -> None:
    hub = LogHub(tmp_path, blobs=None, max_bytes=10**6, queue_chunks=3, queue_finals=1)
    for name in ("att_a", "att_b"):
        hub.open(name, job_id=J, node_id=N, token=1)
    task = asyncio.create_task(hub.run())
    try:
        hub.finalize("att_a", None)
        for _ in range(100):
            if hub.ended_state("att_a") is not None:
                break
            await asyncio.sleep(0.001)
        hub.finalize("att_b", None)
        assert hub.stats.finals_overflowed == 0  # the slot was given back
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
