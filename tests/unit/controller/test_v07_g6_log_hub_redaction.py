"""V0.7 G6 deliverable 5 (D144), controller side: the log store and live
streams never hold or send a secret value, even when the agent sent one (an
old or misbehaving agent - the agent redacts first).

Real :class:`LogHub` + :class:`BlobStore` in ``tmp_path``:

* a value (and its base64) split across two chunks is masked in the live file
  and in every subscriber event; lengths are preserved, so the agent's offsets
  still address the file (a duplicate chunk is still skipped);
* the final copy of an uploaded log is redacted too;
* the values are in ``ACTIVE`` (our own logs) only while the attempt is live.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshforge.common.hashing import sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.secret_redaction import ACTIVE
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.logs import LogHub

pytestmark = [pytest.mark.unit, pytest.mark.security]

A, J, N = "att_g6", "job_g6", "node_g6"
VALUE = b"controller-side-secret-42"
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


async def _drain(hub: LogHub) -> None:
    for _ in range(100):
        if hub._queue.empty():
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_live_file_events_and_final_copy_are_redacted(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs", max_blob_bytes=10**6)
    blobs.prepare()
    hub = LogHub(tmp_path / "logs", blobs=blobs, max_bytes=10**6)
    hub.prepare()
    hub.open(A, job_id=J, node_id=N, token=1, secret_values={"API_KEY": VALUE})
    task = asyncio.create_task(hub.run())
    try:
        sub = hub.subscribe(A, J)
        assert sub is not None
        assert "API_KEY" in list(ACTIVE.names())
        text = b"start " + VALUE + b" mid " + base64.b64encode(VALUE) + b" end\n"
        cut = 6 + len(VALUE) // 2  # inside the raw value
        first, second = text[:cut], text[cut:]
        await hub.on_chunk(LIVE, _msg(0, first))  # type: ignore[arg-type]
        await hub.on_chunk(LIVE, _msg(0, first))  # type: ignore[arg-type] duplicate
        await hub.on_chunk(LIVE, _msg(len(first), second))  # type: ignore[arg-type]
        await _drain(hub)
        final = b"FINAL " + VALUE.hex().encode() + b"\n"
        digest = sha256_bytes(final)

        async def body() -> AsyncIterator[bytes]:
            yield final

        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        await blobs.put(digest, body())
        hub.finalize(A, None)  # first without a final copy: held tail released
        await _drain(hub)
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        live_before_final = hub.path(A, "stdout").read_bytes()
        streamed = b"".join(e.data for e in events if e.kind == "data")
        for blob in (live_before_final, streamed):
            assert len(blob) == len(text)  # length-preserving: offsets still hold
            assert VALUE not in blob and base64.b64encode(VALUE) not in blob
            assert blob.startswith(b"start ") and blob.endswith(b" end\n")
            assert b"[secret:API_KEY]" in blob
        pos = 0
        for event in (e for e in events if e.kind == "data"):
            assert event.file_offset == pos  # contiguous file positions
            pos += len(event.data)
        assert "API_KEY" not in list(ACTIVE.names())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_final_copy_is_redacted(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs", max_blob_bytes=10**6)
    blobs.prepare()
    hub = LogHub(tmp_path / "logs", blobs=blobs, max_bytes=10**6)
    hub.prepare()
    hub.open(A, job_id=J, node_id=N, token=1, secret_values={"API_KEY": VALUE})
    task = asyncio.create_task(hub.run())
    try:
        final = b"FINAL " + VALUE.hex().encode() + b" " + VALUE + b"\n"
        digest = sha256_bytes(final)

        async def body() -> AsyncIterator[bytes]:
            yield final

        await blobs.put(digest, body())
        hub.finalize(A, {"stdout": {"digest": str(digest), "bytes": len(final)}})
        await _drain(hub)
        for _ in range(100):
            if hub.ended_state(A) is not None:
                break
            await asyncio.sleep(0.01)
        copied = hub.path(A, "stdout").read_bytes()
        assert len(copied) == len(final)
        assert VALUE not in copied and VALUE.hex().encode() not in copied
        assert copied.startswith(b"FINAL [secret:API_KEY]")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
