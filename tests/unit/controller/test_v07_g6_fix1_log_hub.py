"""V0.7 G6 fix round 1 (D162): the controller's log hub with the new matcher.

* T-3: a short line of a job that has secrets is written and pushed to
  followers at once - nothing waits for later output;
* T-2: two values that overlap in the output (split over two chunks) leave no
  byte of either in the live file or the followers' events; lengths and file
  offsets stay exact (length-preserving);
* T-8: both streams of an attempt share one matcher.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.secret_redaction import ACTIVE, SecretMatcher
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.logs import LogEvent, LogHub

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

A, J, N = "att_fix1", "job_fix1", "node_fix1"
LIVE = SimpleNamespace(node_id=N)


def _msg(offset: int, data: bytes) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        log_chunk=agent_pb2.LogChunk(
            attempt_id=A,
            job_id=J,
            stream=agent_pb2.LogChunk.STREAM_STDOUT,
            offset=offset,
            data=data,
            fencing_token=1,
        )
    )


def _hub(tmp_path: Path) -> LogHub:
    blobs = BlobStore(tmp_path / "blobs", max_blob_bytes=10**6)
    blobs.prepare()
    hub = LogHub(tmp_path / "logs", blobs=blobs, max_bytes=10**6)
    hub.prepare()
    return hub


async def _drain(hub: LogHub) -> None:
    for _ in range(100):
        if hub._queue.empty() and not hub.busy:  # D168 T-12: off-loop redaction
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.001)


def _events(sub: object) -> list[LogEvent]:
    out: list[LogEvent] = []
    queue = sub.queue  # type: ignore[attr-defined]
    while not queue.empty():
        event = queue.get_nowait()
        if event.kind == "data":
            out.append(event)
    return out


async def test_a_short_line_reaches_the_file_and_followers_at_once(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    secrets = {"API_KEY": b"a-typical-api-key-0123456789abcdef"}
    hub.open(A, job_id=J, node_id=N, token=1, secret_values=secrets)
    task = asyncio.create_task(hub.run())
    try:
        sub = hub.subscribe(A, J)
        assert sub is not None
        line = b"training started, epoch 1\n"
        await hub.on_chunk(LIVE, _msg(0, line))  # type: ignore[arg-type]
        await _drain(hub)
        (event,) = _events(sub)
        assert (event.file_offset, event.data) == (0, line)
        assert hub.path(A, "stdout").read_bytes() == line
    finally:
        hub.finalize(A, None)
        await _drain(hub)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_overlapping_values_over_two_chunks_leave_nothing(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    matcher = SecretMatcher({"A": b"abcd1234-first", "B": b"first-wxyz9876"})
    hub.open(A, job_id=J, node_id=N, token=1, secret_values=matcher)
    task = asyncio.create_task(hub.run())
    try:
        state = hub.live_state(A)
        assert state is not None
        # T-8: one matcher, per-stream state only.
        assert {id(r._m) for r in state.redactors.values()} == {id(matcher)}
        sub = hub.subscribe(A, J)
        assert sub is not None
        text = b"go abcd1234-first-wxyz9876 done\n"
        cut = text.index(b"first") + 2
        await hub.on_chunk(LIVE, _msg(0, text[:cut]))  # type: ignore[arg-type]
        await hub.on_chunk(LIVE, _msg(cut, text[cut:]))  # type: ignore[arg-type]
        await _drain(hub)
        events = _events(sub)
        streamed = b"".join(e.data for e in events)
        written = hub.path(A, "stdout").read_bytes()
        expect = b"go [secret:A]" + b"*" * 13 + b" done\n"
        assert len(expect) == len(text)
        assert streamed == written == expect
        pos = 0
        for event in events:
            assert event.file_offset == pos
            pos += len(event.data)
    finally:
        hub.finalize(A, None)
        await _drain(hub)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert f"logs:{A}" not in ACTIVE._by_holder


async def test_a_stopped_hub_forgets_values_and_a_restarted_one_still_redacts(
    tmp_path: Path,
) -> None:
    """D162 (T-10): stopping the writer removes live attempts' values from the
    process-wide ``ACTIVE`` set (other attempts' holders stay); the redactors
    stay armed, so a writer started again never writes a value in clear."""
    from meshforge.common.logging import scrub_text

    value = b"stop-and-go-secret-9"
    hub = _hub(tmp_path)
    other = "logs:att_other_holder"
    ACTIVE.add(other, {"OTHER": b"other-holder-value"})
    try:
        hub.open(A, job_id=J, node_id=N, token=1, secret_values={"K": value})
        hub.open("att_plain", job_id=J, node_id=N, token=1)
        assert scrub_text(f"x {value.decode()} y") == "x [secret:K] y"
        first = asyncio.create_task(hub.run())
        await asyncio.sleep(0.02)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert f"logs:{A}" not in ACTIVE._by_holder
        assert other in ACTIVE._by_holder
        assert scrub_text(f"x {value.decode()} y") == f"x {value.decode()} y"
        # The same hub's writer again: scrubbing is back, chunks are redacted.
        second = asyncio.create_task(hub.run())
        try:
            await asyncio.sleep(0.02)
            assert f"logs:{A}" in ACTIVE._by_holder
            await hub.on_chunk(LIVE, _msg(0, b"v=" + value + b"\n"))  # type: ignore[arg-type]
            await _drain(hub)
            written = hub.path(A, "stdout").read_bytes()
            assert value not in written and written.startswith(b"v=[secret:K]")
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        assert f"logs:{A}" not in ACTIVE._by_holder
    finally:
        ACTIVE.remove(other)
        ACTIVE.remove(f"logs:{A}")
