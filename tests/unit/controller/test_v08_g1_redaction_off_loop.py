"""V0.8 G1 item 0 (D168 T-12, THREAT_MODEL RE-D2): live log redaction on the
controller runs in a worker thread, so a job whose secret values make the
matcher slow cannot stall the event loop every session shares - and what is
written and streamed is byte-for-byte what the on-loop redaction produced.

Real :class:`LogHub` in ``tmp_path``; the expensive case is the measured
worst case of RE-D2 (periodic secrets against periodic output).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.secret_redaction import SecretMatcher, redact_bytes
from meshforge.controller.jobs.logs import MAX_CHUNK_BYTES, LogHub

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

A, J, N = "att_t12", "job_t12", "node_t12"
LIVE = SimpleNamespace(node_id=N)
SLOW_SECRETS = {f"S{i}": b"a" * (8192 - i) for i in range(32)}
"""RE-D2's worst case: ~0.4 s per 64 KiB chunk of ``a`` on the reference laptop."""


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


async def _idle(hub: LogHub, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not (hub._queue.empty() and not hub.busy):
        assert time.monotonic() < deadline, "the log writer never went idle"
        await asyncio.sleep(0.002)


async def test_a_slow_redaction_does_not_block_the_event_loop(tmp_path: Path) -> None:
    matcher = await asyncio.to_thread(SecretMatcher, SLOW_SECRETS)
    chunk = b"a" * MAX_CHUNK_BYTES
    # How long one chunk costs on this machine (on the loop, before the hub).
    started = time.perf_counter()
    redact_bytes(chunk, matcher, length_preserving=True)
    cost_s = time.perf_counter() - started
    if cost_s < 0.06:
        pytest.skip(f"redaction too fast here to measure a stall ({cost_s:.3f} s)")

    matcher = await asyncio.to_thread(SecretMatcher, SLOW_SECRETS)  # fresh state
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**7)
    hub.prepare()
    hub.open(A, job_id=J, node_id=N, token=1, secret_values=matcher)
    writer = asyncio.create_task(hub.run())
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:  # "another request": wants the loop every 2 ms
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0.02)
        for i in range(3):
            await hub.on_chunk(LIVE, _msg(i * len(chunk), chunk))  # type: ignore[arg-type]
        await _idle(hub)
        stop.set()
        await tick
    finally:
        writer.cancel()
        await asyncio.gather(writer, tick, return_exceptions=True)
    assert hub.stats.redactions_off_loop == 3
    worst = max(gaps)
    # On the loop, the ticker waited a whole chunk's cost (>= cost_s) at least
    # once; off the loop it only waits for GIL switches.
    assert worst < cost_s / 2, (worst, cost_s)
    assert worst < 0.1, worst


async def test_off_loop_redaction_output_is_unchanged(tmp_path: Path) -> None:
    secrets = {"API_KEY": b"controller-side-secret-42", "OTHER": b"0123456789abcdef"}
    text = (
        b"start "
        + secrets["API_KEY"]
        + b" mid "
        + secrets["OTHER"].hex().encode()
        + b" "
        + b"x" * 5000
        + secrets["API_KEY"]
        + b" end\n"
    )
    expected = redact_bytes(text, secrets, length_preserving=True)
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**7)
    hub.prepare()
    hub.open(A, job_id=J, node_id=N, token=1, secret_values=secrets)
    sub = hub.subscribe(A, J)
    assert sub is not None
    writer = asyncio.create_task(hub.run())
    try:
        # Split everywhere awkward: inside each value and at odd sizes.
        cuts = [0, 3, 10, 17, 40, 41, 900, 5050, len(text)]
        for lo, hi in itertools.pairwise(cuts):
            await hub.on_chunk(LIVE, _msg(lo, text[lo:hi]))  # type: ignore[arg-type]
        await hub.on_chunk(LIVE, _msg(0, text[:10]))  # type: ignore[arg-type] duplicate
        await _idle(hub)
        hub.finalize(A, None)  # releases the held tail
        await _idle(hub)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
    live = hub.path(A, "stdout").read_bytes()
    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    streamed = b"".join(e.data for e in events if e.kind == "data")
    assert live == expected
    assert streamed == expected
    pos = 0
    for event in (e for e in events if e.kind == "data"):
        assert event.file_offset == pos
        pos += len(event.data)
    assert hub.stats.redactions_off_loop >= len(cuts) - 1
