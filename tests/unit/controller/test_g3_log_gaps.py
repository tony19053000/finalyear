"""V0.5 G3 (D94): log gaps survive a controller restart.

A real :class:`LogHub` writes into ``tmp_path``; a "restart" is a second hub on
the same folder (the first one's memory is gone, exactly what a new process
sees). The sidecar codec is pinned on its own: every bound has a max-accepted
and a max+1-refused case, and a broken sidecar is ignored, never trusted.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.log_gaps import (
    MAX_SIDECAR_BYTES,
    GapRecord,
    StreamGaps,
    decode,
    encode,
)
from meshforge.controller.jobs.logs import MAX_GAPS_KEPT, STREAMS, LogHub, StreamMerge

pytestmark = pytest.mark.unit

J, N = "job_1", "node_1"
LIVE = SimpleNamespace(node_id=N)


def _msg(attempt: str, offset: int, data: bytes) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        log_chunk=agent_pb2.LogChunk(
            attempt_id=attempt,
            job_id=J,
            stream=agent_pb2.LogChunk.STREAM_STDOUT,
            offset=offset,
            data=data,
            fencing_token=2,
        )
    )


class Hubs:
    """Start/stop hubs on one folder; each start is a "new process"."""

    def __init__(self, root: Path, blobs: BlobStore) -> None:
        self.root = root
        self.blobs = blobs
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> LogHub:
        hub = LogHub(self.root, blobs=self.blobs, max_bytes=10_000)
        hub.prepare()
        self.tasks.append(asyncio.create_task(hub.run()))
        return hub

    async def stop_all(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()


@pytest_asyncio.fixture
async def hubs(tmp_path: Path) -> AsyncIterator[Hubs]:
    blobs = BlobStore(tmp_path / "blobs", max_blob_bytes=10**6)
    blobs.prepare()
    made = Hubs(tmp_path / "logs", blobs)
    yield made
    await made.stop_all()


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition not reached")


async def _feed(hub: LogHub, attempt: str, *chunks: tuple[int, bytes]) -> None:
    before = hub.stats.chunks_appended + hub.stats.chunks_duplicate
    for offset, data in chunks:
        await hub.on_chunk(LIVE, _msg(attempt, offset, data))  # type: ignore[arg-type]
    await _until(
        lambda: (
            hub.stats.chunks_appended + hub.stats.chunks_duplicate
            >= before + len(chunks)
        )
    )


async def _end(hub: LogHub, attempt: str, logs: Any = None) -> None:
    hub.finalize(attempt, logs)
    await _until(lambda: hub.ended_state(attempt) is not None)


# ------------------------------------------------------------- restart


@pytest.mark.asyncio
async def test_an_ended_attempts_gaps_are_reported_after_a_restart(
    hubs: Hubs,
) -> None:
    attempt = str(new_attempt_id())
    first = hubs.start()
    first.open(attempt, job_id=J, node_id=N, token=2)
    await _feed(first, attempt, (0, b"abc"), (10, b"xyz"))
    await _end(first, attempt)
    await hubs.stop_all()

    second = hubs.start()
    state = second.ended_state(attempt)
    assert state is not None and state.ended
    assert state.gaps() == {"stdout": [[3, 10]]}
    assert state.final == {}
    assert second.live_state(attempt) is None  # read back as ended, not live


@pytest.mark.asyncio
async def test_a_live_attempt_continues_at_the_right_offset_after_a_restart(
    hubs: Hubs,
) -> None:
    attempt = str(new_attempt_id())
    first = hubs.start()
    first.open(attempt, job_id=J, node_id=N, token=2)
    await _feed(first, attempt, (0, b"abc"), (10, b"xyz"))
    await hubs.stop_all()  # crash: no finalize

    second = hubs.start()
    second.open(attempt, job_id=J, node_id=N, token=2)  # the Reconcile re-opens
    # The agent replays its last chunk, then continues: no phantom gap.
    await _feed(second, attempt, (10, b"xyz"), (13, b"!"))
    state = second.live_state(attempt)
    assert state is not None
    assert state.gaps() == {"stdout": [[3, 10]]}
    assert state.streams["stdout"].received_to == 14
    assert second.stats.gaps == 0 and second.stats.chunks_duplicate == 1
    assert second.path(attempt, "stdout").read_bytes() == b"abcxyz!"
    await _end(second, attempt)
    third = hubs.start()
    ended = third.ended_state(attempt)
    assert ended is not None and ended.gaps() == {"stdout": [[3, 10]]}


@pytest.mark.asyncio
async def test_an_attempt_ended_right_after_a_restart_keeps_its_gaps(
    hubs: Hubs,
) -> None:
    attempt = str(new_attempt_id())
    first = hubs.start()
    first.open(attempt, job_id=J, node_id=N, token=2)
    await _feed(first, attempt, (5, b"late"))
    await hubs.stop_all()
    second = hubs.start()  # the orphan sweep loses it without a re-open
    second.finalize(attempt, None)
    await _until(lambda: second.ended_state(attempt) is not None)
    third = hubs.start()
    state = third.ended_state(attempt)
    assert state is not None and state.gaps() == {"stdout": [[0, 5]]}


@pytest.mark.asyncio
async def test_a_final_log_that_replaces_the_gapped_stream_drops_the_sidecar(
    hubs: Hubs,
) -> None:
    attempt = str(new_attempt_id())
    complete = b"0123456789xyz"
    digest = sha256_bytes(complete)

    async def body() -> AsyncIterator[bytes]:
        yield complete

    await hubs.blobs.put(digest, body())
    hub = hubs.start()
    hub.open(attempt, job_id=J, node_id=N, token=2)
    await _feed(hub, attempt, (0, b"012"), (10, b"xyz"))
    assert hub.gaps_path(attempt).exists()
    await _end(hub, attempt, {"stdout": {"digest": str(digest)}})
    assert not hub.gaps_path(attempt).exists()
    restarted = hubs.start()
    assert restarted.ended_state(attempt) is None  # nothing left to report


@pytest.mark.asyncio
async def test_no_gap_no_sidecar(hubs: Hubs) -> None:
    attempt = str(new_attempt_id())
    hub = hubs.start()
    hub.open(attempt, job_id=J, node_id=N, token=2)
    await _feed(hub, attempt, (0, b"abc"), (3, b"def"))
    await _end(hub, attempt)
    assert not hub.gaps_path(attempt).exists()


@pytest.mark.asyncio
async def test_a_sidecar_write_failure_is_counted_and_the_writer_carries_on(
    hubs: Hubs,
) -> None:
    attempt = str(new_attempt_id())
    hub = hubs.start()
    hub.gaps_path(attempt).mkdir()  # os.replace onto a folder fails
    hub.open(attempt, job_id=J, node_id=N, token=2)
    await _feed(hub, attempt, (0, b"a"), (5, b"b"), (9, b"c"))
    assert hub.stats.gaps_persist_failures == 2
    assert hub.path(attempt, "stdout").read_bytes() == b"abc"
    assert not [p for p in hub.root.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"v": 2}',
        b" " * (MAX_SIDECAR_BYTES + 1),
    ],
)
@pytest.mark.asyncio
async def test_a_broken_sidecar_is_ignored(hubs: Hubs, content: bytes) -> None:
    attempt = str(new_attempt_id())
    hub = hubs.start()
    hub.gaps_path(attempt).write_bytes(content)
    assert hub.ended_state(attempt) is None
    hub.open(attempt, job_id=J, node_id=N, token=2)
    state = hub.live_state(attempt)
    assert state is not None and state.gaps() == {}
    assert hub.stats.gaps_unreadable == 2


@pytest.mark.asyncio
async def test_a_symlinked_sidecar_is_not_followed(hubs: Hubs, tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlinks need privileges on Windows")
    attempt = str(new_attempt_id())
    record = GapRecord(J, N, 2, True, {"stdout": StreamGaps(((0, 4),), 1, 4)})
    target = tmp_path / "elsewhere.json"
    target.write_bytes(encode(record))
    hub = hubs.start()
    hub.gaps_path(attempt).symlink_to(target)
    assert hub.ended_state(attempt) is None
    assert hub.stats.gaps_unreadable == 1


# ------------------------------------------------------------- codec


def _record(gaps: int, lost: int | None = None) -> GapRecord:
    kept = tuple((2 * i, 2 * i + 1) for i in range(gaps))
    return GapRecord(
        job_id=J,
        node_id=N,
        fencing_token=2,
        ended=True,
        streams={"stdout": StreamGaps(kept, gaps, gaps if lost is None else lost)},
    )


def test_codec_round_trip_and_the_kept_gaps_bound() -> None:
    full = _record(MAX_GAPS_KEPT)
    data = encode(full)
    assert len(data) <= MAX_SIDECAR_BYTES
    assert decode(data, streams=STREAMS, max_gaps=MAX_GAPS_KEPT) == full
    with pytest.raises(ValueError, match="gaps"):
        decode(
            encode(_record(MAX_GAPS_KEPT + 1)),
            streams=STREAMS,
            max_gaps=MAX_GAPS_KEPT,
        )


def _body(**over: Any) -> bytes:
    body = json.loads(encode(_record(1)))
    for key, value in over.items():
        if key.startswith("s_"):
            body["streams"]["stdout"][key[2:]] = value
        else:
            body[key] = value
    return json.dumps(body).encode()


@pytest.mark.parametrize(
    "data",
    [
        _body(fencing_token=True),  # a bool is not an int
        _body(fencing_token=-1),
        _body(fencing_token=2**63),
        _body(job_id=""),
        _body(job_id="j" * 65),
        _body(ended=1),
        _body(streams={"stdin": {}}),
        _body(s_gaps=[[3, 3]]),  # empty
        _body(s_gaps=[[5, 9], [1, 2]]),  # out of order
        _body(s_gaps=[[0, 1, 2]]),
        _body(s_gaps=[[0, 2]], s_lost=1),  # lost below the kept widths
        _body(s_gaps_total=0),  # total below the kept count
        _body(s_final="yes"),
    ],
    ids=[
        "token_bool",
        "token_negative",
        "token_2_63",
        "job_id_empty",
        "job_id_65",
        "ended_int",
        "unknown_stream",
        "gap_empty",
        "gaps_unordered",
        "gap_triple",
        "lost_below_widths",
        "total_below_kept",
        "final_str",
    ],
)
def test_codec_refuses_every_malformed_field(data: bytes) -> None:
    with pytest.raises(ValueError):
        decode(data, streams=STREAMS, max_gaps=MAX_GAPS_KEPT)


def test_codec_id_and_size_bounds_max_accepted() -> None:
    decode(_body(job_id="j" * 64), streams=STREAMS, max_gaps=MAX_GAPS_KEPT)
    decode(_body(fencing_token=2**63 - 1), streams=STREAMS, max_gaps=MAX_GAPS_KEPT)
    padded = _body() + b" " * (MAX_SIDECAR_BYTES - len(_body()))
    assert len(padded) == MAX_SIDECAR_BYTES
    decode(padded, streams=STREAMS, max_gaps=MAX_GAPS_KEPT)
    with pytest.raises(ValueError, match="too large"):
        decode(padded + b" ", streams=STREAMS, max_gaps=MAX_GAPS_KEPT)


def test_merge_counts_lost_bytes_also_past_the_kept_list() -> None:
    m = StreamMerge()
    offset = 0
    for _ in range(MAX_GAPS_KEPT + 3):
        offset += 2
        m.accept(offset, b"x")
        offset += 1
    assert len(m.gaps) == MAX_GAPS_KEPT and m.gaps_total == MAX_GAPS_KEPT + 3
    assert m.lost == 2 * (MAX_GAPS_KEPT + 3)
    assert m.received_to == m.file_len + m.lost


@pytest.mark.asyncio
async def test_gc_takes_the_sidecar_with_the_logs(hubs: Hubs) -> None:
    import datetime as dt

    attempt = str(new_attempt_id())
    hub = hubs.start()
    hub.open(attempt, job_id=J, node_id=N, token=2)
    await _feed(hub, attempt, (4, b"x"))
    await _end(hub, attempt)
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=30)).timestamp()
    for path in (hub.path(attempt, "stdout"), hub.gaps_path(attempt)):
        os.utime(path, (old, old))

    async def expired(_attempt_id: str) -> bool:
        return True

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
    assert await hub.collect_garbage(expired, older_than=cutoff, limit=10) == 2
    assert not hub.gaps_path(attempt).exists()
    assert hub.ended_state(attempt) is None
