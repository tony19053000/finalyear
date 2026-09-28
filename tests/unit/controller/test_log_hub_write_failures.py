"""V0.4 G5 fix round (reviewer B1): a failed live-log write never stops the hub.

The writer task is spawned by ``ControllerRuntime._spawn``; an exception escaping
it stops the controller. A full disk, a permission error or a symlink planted at
the log path (``O_NOFOLLOW`` -> ``ELOOP``) must instead become a counted, logged
failure plus a recorded gap, and the hub must keep serving other attempts.
"""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest
import structlog

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.jobs.logs import LogHub

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
    """Wraps the real live-file handle; raises ENOSPC while ``full`` is set."""

    def __init__(self, fh: IO[bytes], disk: dict[str, Any]) -> None:
        self._fh, self._disk = fh, disk

    def write(self, data: bytes) -> int:
        if self._disk["full"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self._disk.get("short_then_full"):
            self._disk["short_then_full"] = False
            self._disk["full"] = True
            return int(self._fh.write(data[:2]) or 0)
        return int(self._fh.write(data) or 0)

    def close(self) -> None:
        self._fh.close()


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"full": False, "opens": 0}
    real = LogHub._open_live

    def fake_open(self: LogHub, attempt_id: str, stream: str) -> IO[bytes]:
        state["opens"] += 1
        return _Disk(real(self, attempt_id, stream), state)  # type: ignore[return-value]

    monkeypatch.setattr(LogHub, "_open_live", fake_open)
    return state


async def test_enospc_becomes_a_gap_and_the_hub_keeps_serving(
    tmp_path: Path, disk: dict[str, Any]
) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**6)
    hub.prepare()
    hub.open("att_a", job_id=J, node_id=N, token=1)
    hub.open("att_b", job_id=J, node_id=N, token=1)
    task = asyncio.create_task(hub.run())
    try:
        with structlog.testing.capture_logs() as logged:
            await hub.on_chunk(LIVE, _msg("att_a", 0, b"hello "))  # type: ignore[arg-type]
            await _drain(hub)
            disk["full"] = True
            await hub.on_chunk(LIVE, _msg("att_a", 6, b"lost-1"))  # type: ignore[arg-type]
            await hub.on_chunk(LIVE, _msg("att_a", 12, b"lost-2"))  # type: ignore[arg-type]
            await _drain(hub)
            assert not task.done(), "the writer task must survive a failed write"
            disk["full"] = False
            await hub.on_chunk(LIVE, _msg("att_a", 18, b"back"))  # type: ignore[arg-type]
            await hub.on_chunk(LIVE, _msg("att_b", 0, b"other"))  # type: ignore[arg-type]
            await _drain(hub)
            assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert hub.path("att_a", "stdout").read_bytes() == b"hello back"
    assert hub.path("att_b", "stdout").read_bytes() == b"other"
    state = hub.live_state("att_a")
    assert state is not None
    # the two consecutive lost chunks are one recorded range
    assert state.gaps() == {"stdout": [[6, 18]]}
    assert state.streams["stdout"].file_len == len(b"hello back")
    assert hub.stats.write_failures == 2 and hub.stats.gaps == 1
    assert disk["opens"] == 4  # a, a (after close), a (recovered), b
    failed = [e for e in logged if e["event"] == "controller.logs.write_failed"]
    assert len(failed) == 1  # once per failing run, not per chunk
    assert failed[0]["cause"] == "OSError" and failed[0]["errno"] == errno.ENOSPC
    rendered = repr(failed)
    assert "lost-1" not in rendered and str(tmp_path) not in rendered


async def test_a_short_write_records_only_the_unwritten_tail(
    tmp_path: Path, disk: dict[str, Any]
) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**6)
    hub.prepare()
    hub.open("att_a", job_id=J, node_id=N, token=1)
    task = asyncio.create_task(hub.run())
    try:
        disk["short_then_full"] = True  # 2 bytes land, the rest hits ENOSPC
        await hub.on_chunk(LIVE, _msg("att_a", 0, b"abcdef"))  # type: ignore[arg-type]
        await _drain(hub)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert hub.path("att_a", "stdout").read_bytes() == b"ab"
    state = hub.live_state("att_a")
    assert state is not None and state.gaps() == {"stdout": [[2, 6]]}
    assert state.streams["stdout"].file_len == 2
    assert hub.stats.chunks_appended == 1  # the written part was appended


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
async def test_a_planted_symlink_is_never_followed(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**6)
    hub.prepare()
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    hub.path("att_a", "stdout").symlink_to(victim)
    hub.open("att_a", job_id=J, node_id=N, token=1)
    hub.open("att_b", job_id=J, node_id=N, token=1)
    task = asyncio.create_task(hub.run())
    try:
        await hub.on_chunk(LIVE, _msg("att_a", 0, b"evil"))  # type: ignore[arg-type]
        await hub.on_chunk(LIVE, _msg("att_b", 0, b"fine"))  # type: ignore[arg-type]
        await _drain(hub)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert victim.read_bytes() == b"precious"
    assert hub.path("att_b", "stdout").read_bytes() == b"fine"
    state = hub.live_state("att_a")
    assert state is not None and state.gaps() == {"stdout": [[0, 4]]}
    assert hub.stats.write_failures == 1


async def test_a_restart_does_not_trust_a_symlinked_live_file(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**6)
    hub.prepare()
    victim = tmp_path / "victim"
    victim.write_bytes(b"x" * 50)
    hub.path("att_a", "stdout").symlink_to(victim)
    hub.open("att_a", job_id=J, node_id=N, token=1)
    state = hub.live_state("att_a")
    assert state is not None and state.streams["stdout"].received_to == 0
