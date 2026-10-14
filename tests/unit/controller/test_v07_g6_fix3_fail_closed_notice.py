"""V0.7 G6 fix round 3, T-22 (D164), controller side: a redactor that failed
closed is visible.

A loop guard of the matcher fired (forced here): the stream's output is ``*``.
Real :class:`LogHub` + :class:`BlobStore` in ``tmp_path``:

* exactly one warning ``controller.logs.redaction_failed_closed`` per attempt
  and stream, with ids only;
* the note is written OVER the first withheld bytes - the length never
  changes, so the agent's offsets still address the file - also when the
  output arrives in tiny chunks, and in the final copy;
* no value or form is in the file, the events or the log line.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from meshforge.common import secret_redaction as sr
from meshforge.common.hashing import sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.redaction_notice import FAIL_CLOSED_NOTE, FailClosedNotice
from meshforge.common.secret_redaction import (
    RedactionStallError,
    SecretMatcher,
    StreamRedactor,
    encoded_forms,
)
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.logs import LogHub

pytestmark = pytest.mark.unit

A, J, N = "att_f3", "job_f3", "node_f3"
VALUE = b"fail-closed-secret-77"
LIVE = SimpleNamespace(node_id=N)
EVENT = "controller.logs.redaction_failed_closed"
OUT, ERR = agent_pb2.LogChunk.STREAM_STDOUT, agent_pb2.LogChunk.STREAM_STDERR


def _msg(offset: int, data: bytes, stream: int = OUT) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        log_chunk=agent_pb2.LogChunk(
            attempt_id=A,
            job_id=J,
            stream=stream,  # type: ignore[arg-type]
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


@pytest.fixture
def stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every matcher's walker reports a stall (the guard, forced)."""

    def run(self: Any, limit: int) -> None:
        raise RedactionStallError("injected")

    monkeypatch.setattr(sr._Walker, "run", run)


def _clean(blob: bytes) -> None:
    for form in encoded_forms(VALUE):
        assert form not in blob


def _hub(tmp_path: Path) -> tuple[LogHub, BlobStore]:
    blobs = BlobStore(tmp_path / "blobs", max_blob_bytes=10**6)
    blobs.prepare()
    hub = LogHub(tmp_path / "logs", blobs=blobs, max_bytes=10**6)
    hub.prepare()
    hub.open(A, job_id=J, node_id=N, token=1, secret_values={"API_KEY": VALUE})
    return hub, blobs


def test_the_notice_fires_once_and_writes_the_note_over_stars() -> None:
    matcher = SecretMatcher({"K": VALUE})
    redactor = StreamRedactor(matcher, length_preserving=True)
    notice = FailClosedNotice()
    assert not notice.first(redactor)  # healthy: nothing to say
    matcher.stalled = True
    out = redactor.feed(b"x" * 10)
    assert redactor.failed and out == b"*" * 10
    assert notice.first(redactor)
    assert not notice.first(redactor)
    pieces = [notice.overlay(b"*" * n) for n in (0, 10, 7, 200, 5)]
    assert [len(p) for p in pieces] == [0, 10, 7, 200, 5]
    joined = b"".join(pieces)
    assert joined == FAIL_CLOSED_NOTE + b"*" * (222 - len(FAIL_CLOSED_NOTE))
    assert FailClosedNotice.line() == b"\n" + FAIL_CLOSED_NOTE + b"\n"
    assert b"\n" not in FAIL_CLOSED_NOTE


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.usefixtures("stall")
@pytest.mark.parametrize("step", [1, 5, 4096])
async def test_live_streams_say_so_once_each_and_keep_their_length(
    tmp_path: Path, step: int
) -> None:
    hub, _blobs = _hub(tmp_path)
    task = asyncio.create_task(hub.run())
    text = b"before " + VALUE + b" between " + VALUE.hex().encode() + b" after\n"
    text = text * 3
    try:
        sub = hub.subscribe(A, J)
        assert sub is not None
        with structlog.testing.capture_logs() as logs:
            for stream in (OUT, ERR):
                for offset in range(0, len(text), step):
                    await hub.on_chunk(  # type: ignore[arg-type]
                        LIVE, _msg(offset, text[offset : offset + step], stream)
                    )
                    await _drain(hub)
            hub.finalize(A, None)
            await _drain(hub)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    for stream in ("stdout", "stderr"):
        stored = hub.path(A, stream).read_bytes()
        streamed = b"".join(
            e.data for e in events if e.kind == "data" and e.stream == stream
        )
        for blob in (stored, streamed):
            assert len(blob) == len(text)  # offsets still address the file
            assert blob.startswith(FAIL_CLOSED_NOTE)
            assert set(blob[len(FAIL_CLOSED_NOTE) :]) == {ord("*")}
            assert blob.count(FAIL_CLOSED_NOTE) == 1
            _clean(blob)
    warned = [entry for entry in logs if entry["event"] == EVENT]
    assert sorted(entry["stream"] for entry in warned) == ["stderr", "stdout"]
    for entry in warned:
        assert entry["log_level"] == "warning"
        assert (entry["attempt_id"], entry["job_id"], entry["node_id"]) == (A, J, N)
        assert set(entry) == {
            "event",
            "log_level",
            "attempt_id",
            "job_id",
            "node_id",
            "stream",
        }
    _clean(repr(logs).encode())
    assert hub.stats.redaction_failed_closed == 2


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.usefixtures("stall")
@pytest.mark.parametrize("live_first", [False, True])
async def test_the_final_copy_carries_the_note_and_warns_once_per_stream(
    tmp_path: Path, live_first: bool
) -> None:
    hub, blobs = _hub(tmp_path)
    task = asyncio.create_task(hub.run())
    final = b"FINAL " + VALUE + b" and more output to withhold " * 4 + b"\n"
    digest = sha256_bytes(final)

    async def body() -> AsyncIterator[bytes]:
        yield final

    try:
        await blobs.put(digest, body())
        with structlog.testing.capture_logs() as logs:
            if live_first:
                await hub.on_chunk(LIVE, _msg(0, final[:30]))  # type: ignore[arg-type]
                await _drain(hub)
            hub.finalize(A, {"stdout": {"digest": str(digest), "bytes": len(final)}})
            for _ in range(300):
                if hub.ended_state(A) is not None:
                    break
                await asyncio.sleep(0.01)
        copied = hub.path(A, "stdout").read_bytes()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(copied) == len(final)
    assert copied.startswith(FAIL_CLOSED_NOTE)
    assert set(copied[len(FAIL_CLOSED_NOTE) :]) == {ord("*")}
    _clean(copied)
    warned = [entry for entry in logs if entry["event"] == EVENT]
    assert [entry["stream"] for entry in warned] == ["stdout"]  # once, not twice
    _clean(repr(logs).encode())


@pytest.mark.asyncio
async def test_a_healthy_redactor_says_nothing(tmp_path: Path) -> None:
    hub, _blobs = _hub(tmp_path)
    task = asyncio.create_task(hub.run())
    text = b"before " + VALUE + b" after\n"
    try:
        with structlog.testing.capture_logs() as logs:
            await hub.on_chunk(LIVE, _msg(0, text))  # type: ignore[arg-type]
            await _drain(hub)
            hub.finalize(A, None)
            await _drain(hub)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    stored = hub.path(A, "stdout").read_bytes()
    assert b"[secret:API_KEY]" in stored and FAIL_CLOSED_NOTE not in stored
    assert [entry for entry in logs if entry["event"] == EVENT] == []
    assert hub.stats.redaction_failed_closed == 0


@pytest.mark.asyncio
async def test_a_tail_withheld_at_the_end_is_noted_too(tmp_path: Path) -> None:
    """The other stream stalled the shared matcher while this one held the
    start of a value: its tail is withheld when the attempt ends."""
    hub, _blobs = _hub(tmp_path)
    task = asyncio.create_task(hub.run())
    text = b"healthy output " + VALUE[:-1]  # held: it could become the value
    try:
        with structlog.testing.capture_logs() as logs:
            await hub.on_chunk(LIVE, _msg(0, text))  # type: ignore[arg-type]
            await _drain(hub)
            state = hub._attempts[A]
            held = state.redactors["stdout"].held
            assert held == len(VALUE) - 1
            assert state.matcher is not None
            state.matcher.stalled = True
            hub.finalize(A, None)
            await _drain(hub)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    stored = hub.path(A, "stdout").read_bytes()
    assert len(stored) == len(text)
    assert stored == b"healthy output " + FAIL_CLOSED_NOTE[:held]
    warned = [entry for entry in logs if entry["event"] == EVENT]
    assert [entry["stream"] for entry in warned] == ["stdout"]  # stderr: no bytes
