"""V0.4 G5 tester round 2 (fix B1): concurrent followers during a live-log
write failure.

The implementer's own ``test_log_hub_write_failures.py`` never attaches a
:class:`~meshforge.controller.jobs.logs.Subscription` while a write is
failing - it only checks the file on disk afterwards. A WebSocket follower is
the hub's other consumer and must see a consistent, byte-accurate stream: no
event for bytes that were never actually written, the queue never wedges
while the disk is failing, and a follower on an *unrelated* attempt is not
starved or delayed by another attempt's failing writes (the writer task is
shared, single-threaded and serialises both attempts through one queue).
"""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.jobs.logs import LogEvent, LogHub

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

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
            fencing_token=1,
        )
    )


async def _drain(hub: LogHub) -> None:
    for _ in range(200):
        if hub._queue.empty():
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.001)


class _Disk:
    def __init__(self, fh: IO[bytes], disk: dict[str, Any]) -> None:
        self._fh, self._disk = fh, disk

    def write(self, data: bytes) -> int:
        if self._disk["full"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return int(self._fh.write(data) or 0)

    def close(self) -> None:
        self._fh.close()


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"full": False}
    real = LogHub._open_live

    def fake_open(self: LogHub, attempt_id: str, stream: str) -> IO[bytes]:
        return _Disk(real(self, attempt_id, stream), state)  # type: ignore[return-value]

    monkeypatch.setattr(LogHub, "_open_live", fake_open)
    return state


async def _events(sub: Any, count: int) -> list[LogEvent]:
    return [await asyncio.wait_for(sub.queue.get(), timeout=5) for _ in range(count)]


async def test_a_follower_never_sees_bytes_that_were_never_written(
    tmp_path: Path, disk: dict[str, Any]
) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**6)
    hub.prepare()
    hub.open("att_a", job_id=J, node_id=N, token=1)
    sub = hub.subscribe("att_a", J)
    assert sub is not None
    task = asyncio.create_task(hub.run())
    try:
        await hub.on_chunk(LIVE, _msg("att_a", 0, b"hello "))  # type: ignore[arg-type]
        await _drain(hub)
        first = await _events(sub, 1)
        assert first[0] == LogEvent("data", "stdout", 0, b"hello ")

        disk["full"] = True
        await hub.on_chunk(LIVE, _msg("att_a", 6, b"lost-1"))  # type: ignore[arg-type]
        await hub.on_chunk(LIVE, _msg("att_a", 12, b"lost-2"))  # type: ignore[arg-type]
        await _drain(hub)
        # nothing was pushed for the failed range: the queue is still empty
        assert sub.queue.empty()

        disk["full"] = False
        await hub.on_chunk(LIVE, _msg("att_a", 18, b"back"))  # type: ignore[arg-type]
        await _drain(hub)
        second = await _events(sub, 1)
        # "back" lands right after "hello " *in the file* (6 bytes in): the
        # lost stream range (offsets 6-18) is simply not in the file at all.
        assert second[0] == LogEvent("data", "stdout", 6, b"back")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # what the follower saw, concatenated, equals what actually landed on disk
    assert hub.path("att_a", "stdout").read_bytes() == b"hello back"


async def test_an_unrelated_attempts_follower_is_not_starved_by_a_failing_one(
    tmp_path: Path, disk: dict[str, Any]
) -> None:
    """Both attempts share the one writer task and queue; a run of failing
    writes for ``att_a`` must not delay or drop events for ``att_b``, whose
    writes keep succeeding throughout."""
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**6)
    hub.prepare()
    hub.open("att_a", job_id=J, node_id=N, token=1)
    hub.open("att_b", job_id=J, node_id=N, token=1)
    sub_a = hub.subscribe("att_a", J)
    sub_b = hub.subscribe("att_b", J)
    assert sub_a is not None and sub_b is not None
    task = asyncio.create_task(hub.run())
    try:
        disk["full"] = True
        for i in range(20):
            await hub.on_chunk(LIVE, _msg("att_a", i * 4, f"a{i:03d}".encode()))  # type: ignore[arg-type]
            await _drain(hub)  # let the writer actually attempt (and fail) it
        for i in range(5):
            # att_b's writes must keep landing throughout: the disk is only
            # "full" while the writer is touching att_a's file (the fixture
            # is a shared fake, but each stream's file handle is independent
            # in the real filesystem - here we drop the flag only around each
            # of att_b's own chunks to prove *its* writes are unaffected).
            disk["full"] = False
            await hub.on_chunk(LIVE, _msg("att_b", i * 4, f"b{i:03d}".encode()))  # type: ignore[arg-type]
            await _drain(hub)
            disk["full"] = True

        b_events = await _events(sub_b, 5)
        assert [e.data for e in b_events] == [f"b{i:03d}".encode() for i in range(5)]
        assert sub_a.queue.empty()  # every att_a write failed; nothing to see
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert hub.path("att_b", "stdout").read_bytes() == b"".join(
        f"b{i:03d}".encode() for i in range(5)
    )
    state_a = hub.live_state("att_a")
    assert state_a is not None
    assert state_a.streams["stdout"].file_len == 0
    assert hub.stats.write_failures >= 1
