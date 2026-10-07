"""V0.6 G4 tester round 2 - T-9 on real sockets: a probe target that drips.

The target answers every request with a correct status line and a body it
sends in small pieces, each inside the per-request read timeout. Without a
whole-probe deadline the agent's one probe slot is held for as long as the
body takes (here: far longer than the test waits). With ``deadline_s`` the
probe ends as ``deadline`` and the next probe is measured, not ``busy``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from meshforge.agent.peer.probe import ProbeService
from meshforge.agent.settings import ProbeSettings
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MIB = 1024 * 1024


class Target:
    """A hand-written HTTP/1.1 server (one connection at a time is enough)."""

    def __init__(self) -> None:
        self.drip = True
        self.requests = 0
        self.port = 0
        self._server: asyncio.Server | None = None

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                self.requests += 1
                line = head.split(b"\r\n", 1)[0].decode()
                size = int(line.split("bytes=")[1].split(" ")[0])
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n".encode()
                )
                await writer.drain()
                sent = 0
                while sent < size:
                    piece = min(1024, size - sent)
                    writer.write(bytes(piece))
                    await writer.drain()
                    sent += piece
                    if self.drip and size > 0:
                        await asyncio.sleep(0.1)  # well inside the read timeout
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)


@pytest_asyncio.fixture
async def target() -> AsyncIterator[Target]:
    t = Target()
    await t.start()
    try:
        yield t
    finally:
        await t.stop()


def request(probe_id: str, port: int, size: int = 4 * MIB) -> agent_pb2.ProbeRequest:
    return agent_pb2.ProbeRequest(
        probe_id=probe_id,
        target_node_id="node_b",
        endpoint=f"127.0.0.1:{port}",
        token="tok",
        rtt_samples=3,
        bytes=size,
        max_bytes=2 * size,
        fast_s=0.5,
    )


async def wait_for(sent: list[Any], n: int, limit: float = 15.0) -> None:
    end = time.monotonic() + limit
    while len(sent) < n and time.monotonic() < end:  # noqa: ASYNC110
        await asyncio.sleep(0.05)


async def test_a_dripping_target_cannot_hold_the_probe_slot(target: Target) -> None:
    sent: list[agent_pb2.NetProbeResult] = []

    async def report(_name: str, msg: Any) -> str:
        sent.append(msg)
        return "id"

    service = ProbeService(
        ProbeSettings(request_timeout_s=5.0, deadline_s=1.0), report, node_id="node_a"
    )
    try:
        started = time.monotonic()
        service.handle(request("slow", target.port))
        await wait_for(sent, 1)
        took = time.monotonic() - started
        assert [(r.probe_id, r.ok, r.error) for r in sent] == [
            ("slow", False, "deadline")
        ]
        assert took < 4.0, f"the deadline (1 s) held for {took:.1f} s"
        assert sent[0].bytes < 4 * MIB  # never claims the whole download

        target.drip = False  # the target recovers: the slot must be free
        service.handle(request("next", target.port))
        await wait_for(sent, 2)
        assert (sent[-1].probe_id, sent[-1].ok) == ("next", True)
        assert sent[-1].bw_bytes_s > 0 and sent[-1].bytes >= 4 * MIB
    finally:
        await service.close()


async def test_a_replayed_command_after_a_deadline_repeats_the_failure_not_a_new_probe(
    target: Target,
) -> None:
    """Idempotent by probe id (ARCHITECTURE §13): the controller re-sends the
    command after a reconnect; the agent must say ``deadline`` again, not
    spend another 4 MiB on the target."""
    sent: list[agent_pb2.NetProbeResult] = []

    async def report(_name: str, msg: Any) -> str:
        sent.append(msg)
        return "id"

    service = ProbeService(
        ProbeSettings(request_timeout_s=5.0, deadline_s=0.6), report, node_id="node_a"
    )
    try:
        service.handle(request("p1", target.port))
        await wait_for(sent, 1)
        seen = target.requests
        service.handle(request("p1", target.port))
        await wait_for(sent, 2)
        assert [r.error for r in sent] == ["deadline", "deadline"]
        assert target.requests == seen
    finally:
        await service.close()
