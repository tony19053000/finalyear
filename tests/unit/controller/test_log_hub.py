"""V0.4 G5 D71/D72: live log merge by offset, fencing, final copy, slow followers.

No network: a real :class:`LogHub` writing into ``tmp_path`` and a real
:class:`BlobStore` for the final copy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from meshforge.common.hashing import sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.logs import (
    MAX_CHUNK_BYTES,
    LogEvent,
    LogHub,
    StreamMerge,
)

pytestmark = pytest.mark.unit

A, J, N = "att_1", "job_1", "node_1"


# ------------------------------------------------------------------ merge


def test_merge_duplicate_overlap_gap_reorder() -> None:
    m = StreamMerge()
    assert m.accept(0, b"abc") == b"abc"
    assert m.accept(0, b"abc") == b""  # duplicate
    assert m.accept(1, b"b") == b""  # already covered
    assert m.accept(2, b"cde") == b"de"  # overlap: new tail only
    assert m.accept(8, b"ij") == b"ij"  # gap 5..8 recorded, not filled
    assert m.gaps == [(5, 8)] and m.gaps_total == 1
    assert m.accept(5, b"fgh") == b""  # a late chunk behind the cursor: skipped
    assert m.file_len == len(b"abcdeij") and m.received_to == 10


def test_merge_gap_is_never_filled_with_invented_bytes() -> None:
    m = StreamMerge()
    out = m.accept(0, b"x") + m.accept(100, b"y")
    assert out == b"xy"  # 99 missing bytes are a recorded gap, not zeros
    assert m.gaps == [(1, 100)]


# ------------------------------------------------------------------ hub


@pytest_asyncio.fixture
async def hub(tmp_path: Path) -> AsyncIterator[LogHub]:
    blobs = BlobStore(tmp_path / "blobs", max_blob_bytes=10**6)
    blobs.prepare()
    running = LogHub(tmp_path / "logs", blobs=blobs, max_bytes=1000)
    running.prepare()
    running.open(A, job_id=J, node_id=N, token=2)
    task = asyncio.create_task(running.run())
    yield running
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _msg(
    offset: int,
    data: bytes,
    *,
    attempt: str = A,
    token: int = 2,
    stream: Any = agent_pb2.LogChunk.STREAM_STDOUT,
) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        log_chunk=agent_pb2.LogChunk(
            attempt_id=attempt,
            job_id=J,
            stream=stream,
            offset=offset,
            data=data,
            fencing_token=token,
        )
    )


LIVE = SimpleNamespace(node_id=N)


async def _drain(hub: LogHub) -> None:
    for _ in range(50):
        if hub._queue.empty():
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_chunks_are_appended_by_offset(hub: LogHub) -> None:
    for offset, data in ((0, b"hello "), (0, b"hello "), (6, b"world"), (20, b"!")):
        await hub.on_chunk(LIVE, _msg(offset, data))  # type: ignore[arg-type]
    await _drain(hub)
    assert hub.path(A, "stdout").read_bytes() == b"hello world!"
    state = hub.live_state(A)
    assert state is not None and state.gaps() == {"stdout": [[11, 20]]}


@pytest.mark.asyncio
async def test_stale_token_unknown_attempt_other_node_are_dropped(hub: LogHub) -> None:
    await hub.on_chunk(LIVE, _msg(0, b"old", token=1))  # type: ignore[arg-type]
    await hub.on_chunk(LIVE, _msg(0, b"??", attempt="att_x"))  # type: ignore[arg-type]
    await hub.on_chunk(SimpleNamespace(node_id="node_2"), _msg(0, b"no"))  # type: ignore[arg-type]
    await _drain(hub)
    assert hub.stats.chunks_fenced == 3
    assert not hub.path(A, "stdout").exists()


@pytest.mark.asyncio
async def test_bounds_max_accepted_max_plus_one_refused(hub: LogHub) -> None:
    await hub.on_chunk(LIVE, _msg(1000 - 10, b"z" * 10))  # type: ignore[arg-type]
    await hub.on_chunk(LIVE, _msg(1000 - 10, b"z" * 11))  # type: ignore[arg-type]
    big = b"q" * (MAX_CHUNK_BYTES + 1)
    await hub.on_chunk(LIVE, _msg(0, big, stream=agent_pb2.LogChunk.STREAM_STDERR))  # type: ignore[arg-type]
    await hub.on_chunk(
        LIVE, _msg(0, b"s", stream=agent_pb2.LogChunk.STREAM_UNSPECIFIED)
    )  # type: ignore[arg-type]
    await _drain(hub)
    assert hub.stats.chunks_rejected == 3 and hub.stats.chunks_appended == 1


@pytest.mark.asyncio
async def test_finalize_replaces_the_live_file_with_the_verified_blob(
    hub: LogHub, tmp_path: Path
) -> None:
    await hub.on_chunk(LIVE, _msg(0, b"par"))  # type: ignore[arg-type]
    await hub.on_chunk(LIVE, _msg(10, b"tial"))  # type: ignore[arg-type]
    complete = b"par...+++tial and more\n"
    digest = sha256_bytes(complete)

    async def body() -> AsyncIterator[bytes]:
        yield complete

    assert hub._blobs is not None
    await hub._blobs.put(digest, body())
    sub = hub.subscribe(A, J)
    assert sub is not None
    hub.finalize(A, {"stdout": {"digest": str(digest), "bytes": 23}})
    await _drain(hub)
    await asyncio.wait_for(sub.wake.wait(), 1)
    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    assert events[-1].kind == "end"
    assert hub.path(A, "stdout").read_bytes() == complete
    ended = hub.ended_state(A)
    assert ended is not None and ended.final == {"stdout": True}
    # a chunk after the end is fenced
    await hub.on_chunk(LIVE, _msg(100, b"late"))  # type: ignore[arg-type]
    assert hub.stats.chunks_fenced == 1


@pytest.mark.asyncio
async def test_a_slow_subscriber_is_marked_not_waited_for(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**6, subscriber_queue=8)
    hub.prepare()
    hub.open(A, job_id=J, node_id=N, token=1)
    task = asyncio.create_task(hub.run())
    sub = hub.subscribe(A, J)
    assert sub is not None
    for i in range(20):
        await hub.on_chunk(LIVE, _msg(i, b"x", token=1))  # type: ignore[arg-type]
    await _drain(hub)
    assert sub.overflowed and hub.stats.chunks_appended == 20
    assert hub.stats.slow_subscribers == 1
    # the WebSocket route also finds the same reader slow (a send timed out):
    # still one slow subscriber, not two
    assert sub.mark_slow() is False
    assert hub.stats.slow_subscribers == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_a_reader_found_slow_by_a_send_timeout_first_is_counted_once(
    tmp_path: Path,
) -> None:
    hub = LogHub(tmp_path, blobs=None, max_bytes=10, subscriber_queue=1)
    sub = hub.subscribe(A, J)
    other = hub.subscribe(A, J)
    assert sub is not None and other is not None
    assert sub.mark_slow() is True  # send timeout seen first
    sub.push(_event())
    sub.push(_event())  # then its queue overflows as well
    assert sub.overflowed and hub.stats.slow_subscribers == 1
    assert other.mark_slow() is True  # a second slow reader is its own count
    assert hub.stats.slow_subscribers == 2


def _event() -> LogEvent:
    return LogEvent(kind="data", stream="stdout", file_offset=0, data=b"x")


def test_subscribers_per_job_max_accepted_max_plus_one_refused(tmp_path: Path) -> None:
    hub = LogHub(tmp_path, blobs=None, max_bytes=10, max_subscribers=2)
    first, second = hub.subscribe(A, J), hub.subscribe(A, J)
    assert first is not None and second is not None
    assert hub.subscribe(A, J) is None
    first.close()
    assert hub.subscribe(A, J) is not None
