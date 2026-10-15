"""V0.8 G1 tester r1 (D168 T-12): the off-loop redaction is order-preserving
and secret-safe for any chunking, any interleaving of attempts, and a
finalize racing a chunk that is still in the worker thread."""

# ruff: noqa: S311

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.secret_redaction import redact_bytes
from meshforge.controller.jobs.logs import LogHub

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

SECRETS = {"K1": b"hunter2-super-secret", "K2": b"tok_0123456789abcdef"}
LIVE = SimpleNamespace(node_id="n1")


def _msg(att: str, job: str, offset: int, data: bytes) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        log_chunk=agent_pb2.LogChunk(
            attempt_id=att,
            job_id=job,
            stream=agent_pb2.LogChunk.STREAM_STDOUT,
            offset=offset,
            data=data,
            fencing_token=1,
        )
    )


async def _idle(hub: LogHub) -> None:
    end = time.monotonic() + 30
    while not (hub._queue.empty() and not hub.busy):
        assert time.monotonic() < end
        await asyncio.sleep(0.002)


def _body(rng: random.Random) -> bytes:
    parts = []
    for _ in range(rng.randint(5, 40)):
        parts.append(rng.choice([b"line ", b"x" * rng.randint(1, 300), b"\n", b" "]))
        if rng.random() < 0.4:
            parts.append(rng.choice(list(SECRETS.values())))
    return b"".join(parts)


async def test_any_chunking_and_two_interleaved_attempts_equal_redact_bytes(
    tmp_path: Path,
) -> None:
    rng = random.Random(8)
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**7)
    hub.prepare()
    bodies = {}
    for att in ("att_a", "att_b"):
        bodies[att] = _body(rng)
        hub.open(att, job_id="job_" + att, node_id="n1", token=1, secret_values=SECRETS)
    writer = asyncio.create_task(hub.run())
    try:
        cursors = dict.fromkeys(bodies, 0)
        while any(cursors[a] < len(bodies[a]) for a in bodies):
            att = rng.choice([a for a in bodies if cursors[a] < len(bodies[a])])
            lo = cursors[att]
            hi = min(len(bodies[att]), lo + rng.randint(1, 120))
            await hub.on_chunk(LIVE, _msg(att, "job_" + att, lo, bodies[att][lo:hi]))  # type: ignore[arg-type]
            cursors[att] = hi
            if rng.random() < 0.2:
                await asyncio.sleep(0)
        await _idle(hub)
        for att in bodies:
            hub.finalize(att, None)
        await _idle(hub)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
    for att, body in bodies.items():
        on_disk = hub.path(att, "stdout").read_bytes()
        assert on_disk == redact_bytes(body, SECRETS, length_preserving=True)
        for secret in SECRETS.values():
            assert secret not in on_disk


async def test_finalize_while_a_chunk_is_in_the_thread_never_writes_a_secret(
    tmp_path: Path,
) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=10**7)
    hub.prepare()
    hub.open("att_f", job_id="job_f", node_id="n1", token=1, secret_values=SECRETS)
    writer = asyncio.create_task(hub.run())
    try:
        data = b"before " + SECRETS["K1"][:10]
        await hub.on_chunk(LIVE, _msg("att_f", "job_f", 0, data))  # type: ignore[arg-type]
        hub.finalize("att_f", None)  # races the worker
        await _idle(hub)
        await asyncio.sleep(0.05)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
    path = hub.path("att_f", "stdout")
    written = path.read_bytes() if path.exists() else b""
    assert SECRETS["K1"] not in written
    assert SECRETS["K1"][:10] not in written or written == redact_bytes(
        data, SECRETS, length_preserving=True
    )
