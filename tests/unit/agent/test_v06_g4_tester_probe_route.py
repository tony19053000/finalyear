"""V0.6 G4 tester round 1 (D111): the ``/v1/probe`` route and the probing client
against hostile peers, on real loopback sockets.

Route: malformed sizes, token kinds/holders/expiry, duplicate headers, HEAD, a
slow-loris client, a reader that never reads, many concurrent probes.
Client (:class:`ProbeService`): a target that drips bytes, answers with an
endless body, redirects, resets the connection or lies about its length.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.agent.cas import AgentCas
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.peer.probe import ProbeService
from meshforge.agent.settings import PeerSettings, ProbeSettings
from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_node_id, validate_node_id
from meshforge.common.peer_token import SKEW_S, sign_peer_token, sign_probe_token
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NOW = 1_800_000_000.0
MIB = 1024 * 1024


@dataclass
class Env:
    key: Ed25519PrivateKey
    holder: str
    server: PeerBlobServer
    clock: list[float]

    def token(self, *, ttl_s: int = 60, holder: str | None = None) -> str:
        return sign_probe_token(
            self.key,
            requester=new_node_id(),
            holder=holder or self.holder,
            now=self.clock[0],
            ttl_s=ttl_s,
        )

    @property
    def port(self) -> int:
        return int(self.server.port)


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
    cas.store.prepare()
    key = Ed25519PrivateKey.generate()
    holder = new_node_id()
    clock = [NOW]
    auth = PeerAuthorizer(cas, node_id=holder, wall_clock=lambda: clock[0])
    auth.set_key(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    cfg = PeerSettings.model_validate(
        {"bind": "127.0.0.1", "port": 0, "idle_timeout_s": 1.5, "max_connections": 8}
    )
    server = PeerBlobServer(cas, auth, cfg, node_id=holder)
    await server.start()
    try:
        yield Env(key, holder, server, clock)
    finally:
        await server.close()


async def raw(port: int, data: bytes, *, limit: int = 1 << 16) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        return await asyncio.wait_for(reader.read(limit), 5.0)
    finally:
        writer.close()


def request(
    target: str, token: str | None, method: str = "GET", extra: str = ""
) -> bytes:
    auth = "" if token is None else f"Authorization: Bearer {token}\r\n"
    return (f"{method} {target} HTTP/1.1\r\nHost: x\r\n{auth}{extra}\r\n").encode(
        "latin-1"
    )


def status(response: bytes) -> int:
    return int(response.split(b" ", 2)[1]) if response else 0


@pytest.mark.parametrize(
    "target",
    [
        "/v1/probe",
        "/v1/probe?bytes=",
        "/v1/probe?bytes=-1",
        "/v1/probe?bytes=1.5",
        "/v1/probe?bytes=abc",
        "/v1/probe?bytes=1e9",
        "/v1/probe?bytes=0x10",
        "/v1/probe?bytes=%31",
        "/v1/probe?bytes=1&bytes=2",
        "/v1/probe?bytes=1&x=2",
        "/v1/probe?x=1&bytes=1",
        "/v1/probe/?bytes=1",
        "/v1/probe?bytes=9999999999",
        "/v1/probe?bytes=" + "9" * 5000,
        "/v1/probe?bytes=8388609",
        "/v1/probe?bytes=0008388609",
    ],
)
async def test_malformed_or_oversize_requests_never_get_a_body(
    env: Env, target: str
) -> None:
    out = await raw(env.port, request(target, env.token()))
    assert status(out) in (400, 404, 431, 414), out[:80]
    assert len(out) < 2000
    # the server is still fine
    ok = await raw(env.port, request("/v1/probe?bytes=3", env.token()))
    assert status(ok) == 200 and ok.endswith(b"\x00\x00\x00")


async def test_leading_zeros_and_zero_bytes_are_served_exactly(env: Env) -> None:
    out = await raw(env.port, request("/v1/probe?bytes=000000005", env.token()))
    assert status(out) == 200 and out.endswith(b"\x00" * 5)
    assert b"content-length: 5" in out.lower()
    out = await raw(env.port, request("/v1/probe?bytes=0", env.token()))
    assert status(out) == 200 and b"content-length: 0" in out.lower()


async def test_token_checks_on_the_probe_route(env: Env) -> None:
    good = env.token()
    other = env.token(holder=new_node_id())
    blob = sign_peer_token(
        env.key,
        tree=Digest("ab" * 32),
        requester=new_node_id(),
        holder=validate_node_id(env.holder),
        attempt_id=new_attempt_id(),
        now=NOW,
    )
    target = "/v1/probe?bytes=10"
    assert status(await raw(env.port, request(target, None))) == 401
    assert status(await raw(env.port, request(target, ""))) == 401
    assert status(await raw(env.port, request(target, "garbage"))) == 403
    assert status(await raw(env.port, request(target, other))) == 403
    assert status(await raw(env.port, request(target, blob))) == 403
    assert status(await raw(env.port, request(target, good[:-4] + "AAAA"))) == 403
    basic = f"GET {target} HTTP/1.1\r\nHost: x\r\nAuthorization: Basic {good}\r\n\r\n"
    assert status(await raw(env.port, basic.encode())) == 401
    twice = request(target, good, extra=f"Authorization: Bearer {good}\r\n")
    assert status(await raw(env.port, twice)) == 400
    # expiry: one second after the 60 s life
    env.clock[0] = NOW + 60 + SKEW_S  # the verifier's clock-skew allowance
    assert status(await raw(env.port, request(target, good))) == 200
    env.clock[0] = NOW + 60 + SKEW_S + 1
    assert status(await raw(env.port, request(target, good))) == 403
    env.clock[0] = NOW
    assert status(await raw(env.port, request(target, good))) == 200


async def test_a_probe_token_is_useless_on_the_blob_route(env: Env) -> None:
    digest = "ab" * 32
    out = await raw(env.port, request(f"/v1/blobs/{digest}", env.token()))
    assert status(out) in (401, 403, 404) and status(out) != 200


async def test_head_and_methods(env: Env) -> None:
    out = await raw(env.port, request("/v1/probe?bytes=1000", env.token(), "HEAD"))
    assert status(out) == 200 and b"content-length: 1000" in out.lower()
    assert not out.endswith(b"\x00")
    for method in ("POST", "PUT", "DELETE"):
        out = await raw(env.port, request("/v1/probe?bytes=1", env.token(), method))
        assert status(out) == 405


async def test_slow_loris_is_dropped_within_the_idle_timeout(env: Env) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", env.port)
    started = time.monotonic()
    try:
        writer.write(b"GET /v1/probe?by")  # never finishes the request line
        await writer.drain()
        data = await asyncio.wait_for(reader.read(100), 6.0)
    finally:
        writer.close()
    assert data == b"" or status(data) in (400, 408)
    assert time.monotonic() - started < 5.0


async def test_a_reader_that_never_reads_cannot_hold_a_connection_for_ever(
    env: Env,
) -> None:
    """8 MiB requested, nothing read: the response budget (idle + size / min
    rate = 1.5 + 8 MiB / 64 KiB/s = 129 s) is the longest it may hold the
    connection; check the server accounts it as open and the cap is honoured
    by opening more connections than ``max_connections``."""
    writers = []
    for _ in range(20):
        _reader, writer = await asyncio.open_connection("127.0.0.1", env.port)
        writer.write(request("/v1/probe?bytes=8388608", env.token()))
        await writer.drain()
        writers.append(writer)
    await asyncio.sleep(0.5)
    try:
        assert env.server.stats.connections_rejected >= 12  # 20 asked, cap 8
    finally:
        for w in writers:
            w.close()
    await asyncio.sleep(0.5)
    ok = await raw(env.port, request("/v1/probe?bytes=1", env.token()))
    assert status(ok) == 200, "the server did not recover once the clients left"


# ------------------------------------------------------- the probing client


class Reports:
    def __init__(self) -> None:
        self.items: list[agent_pb2.NetProbeResult] = []

    async def __call__(self, name: str, payload: Any) -> str:
        assert name == "net_probe_result"
        self.items.append(payload)
        return "id"


def msg(port: int, probe_id: str = "p1", **kw: Any) -> agent_pb2.ProbeRequest:
    base: dict[str, Any] = {
        "probe_id": probe_id,
        "target_node_id": "node_target",
        "endpoint": f"127.0.0.1:{port}",
        "token": "SECRET-TOKEN-VALUE",
        "rtt_samples": 1,
        "bytes": 1 * MIB,
        "max_bytes": 2 * MIB,
        "fast_s": 0.5,
    }
    base.update(kw)
    return agent_pb2.ProbeRequest(**base)


async def serve(handler: Any) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


async def read_head(reader: asyncio.StreamReader) -> bytes:
    return await reader.readuntil(b"\r\n\r\n")


async def run_probe(
    handler: Any, wait_s: float = 8.0, timeout_s: float = 0.5
) -> tuple[ProbeService, Reports, list[agent_pb2.NetProbeResult]]:
    server, port = await serve(handler)
    reports = Reports()
    svc = ProbeService(
        ProbeSettings(request_timeout_s=timeout_s), reports, node_id="node_me"
    )
    try:
        svc.handle(msg(port))
        deadline = time.monotonic() + wait_s
        while not reports.items and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    finally:
        await svc.close()
        server.close()
        await server.wait_closed()
    return svc, reports, reports.items


async def drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await read_head(reader)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        )  # rtt requests answered
        await writer.drain()
        await read_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
        for _ in range(100):  # one byte every 0.3 s: under the 0.5 s read timeout
            writer.write(b"\x00")
            await writer.drain()
            await asyncio.sleep(0.3)
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def test_a_dripping_target_cannot_pin_the_probe_slot() -> None:
    """Every read is inside ``request_timeout_s`` but the 1 MiB body would take
    3.5 days. The probe must end (as a failure) in bounded time or this agent
    answers ``busy`` to every later probe."""
    _svc, _reports, got = await run_probe(drip, wait_s=6.0)
    assert got, "no result within 6 s: the agent's only probe slot is pinned"
    assert not got[0].ok


test_a_dripping_target_cannot_pin_the_probe_slot = pytest.mark.xfail(
    strict=True,
    reason="T-9: request_timeout_s bounds each read, not the probe; a slow "
    "target holds the agent's single probe slot ('busy') indefinitely",
)(test_a_dripping_target_cannot_pin_the_probe_slot)


async def test_an_endless_body_is_cut_off_at_the_requested_size() -> None:
    async def endless(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await read_head(reader)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await read_head(reader)
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            chunk = b"10000\r\n" + b"\x00" * 65536 + b"\r\n"
            for _ in range(2000):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    _svc, _reports, got = await run_probe(endless)
    assert got and not got[0].ok and got[0].error == "oversized"
    assert got[0].bytes <= 1 * MIB + 65536 + 4096  # never downloads the endless body


async def test_redirect_reset_and_short_bodies_are_failures_not_hangs() -> None:
    async def redirect(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await read_head(reader)
        writer.write(
            b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/x\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    _s, _r, got = await run_probe(redirect)
    assert got and not got[0].ok and got[0].error == "http_302"

    async def reset(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await read_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\nabc")
        await writer.drain()
        writer.transport.abort()

    _s, _r, got = await run_probe(reset)
    assert got and not got[0].ok and got[0].error in {"unreachable", "short"}
    assert got[0].bytes < 1 * MIB

    async def garbage(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await read_head(reader)
        writer.write(b"\x00\xff not http at all\r\n\r\n")
        await writer.drain()
        writer.close()

    _s, _r, got = await run_probe(garbage)
    assert got and not got[0].ok


async def test_the_token_never_reaches_the_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def refuse(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await read_head(reader)
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    with caplog.at_level(logging.DEBUG):
        _s, _r, got = await run_probe(refuse)
    assert got and got[0].error == "http_403"
    assert "SECRET-TOKEN-VALUE" not in caplog.text


async def test_a_second_probe_while_one_runs_is_answered_busy_and_a_replay_is_idempotent() -> (
    None
):
    release = asyncio.Event()

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await read_head(reader)
            await release.wait()
        finally:
            writer.close()

    server, port = await serve(hold)
    reports = Reports()
    svc = ProbeService(ProbeSettings(request_timeout_s=5.0), reports, node_id="me")
    try:
        svc.handle(msg(port, "first"))
        await asyncio.sleep(0.2)
        svc.handle(msg(port, "second"))
        svc.handle(msg(port, "second"))  # a replay of the refused command
        await asyncio.sleep(0.3)
        busy = [r for r in reports.items if r.probe_id == "second"]
        assert busy and all(r.error == "busy" and not r.ok for r in busy)
        before = len(reports.items)
        svc.handle(msg(port, "first"))  # replay of the running one: ignored
        await asyncio.sleep(0.2)
        assert len(reports.items) == before
    finally:
        release.set()
        await svc.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "bad",
    [
        {"endpoint": "evil.example.com"},
        {"endpoint": "127.0.0.1:0"},
        {"endpoint": "127.0.0.1:70000"},
        {"endpoint": "127.0.0.1:80/../x"},
        {"endpoint": "user@127.0.0.1:80"},
        {"endpoint": "127.0.0.1:80\r\nX: y"},
        {"token": ""},
        {"bytes": 9 * MIB},
        {"max_bytes": 9 * MIB},
        {"rtt_samples": 11},
        {"fast_s": -1.0},
        {"fast_s": 1e9},
        {"probe_id": "bad id with spaces"},
        {"probe_id": "x" * 65},
    ],
)
async def test_bad_commands_never_open_a_connection(bad: dict[str, Any]) -> None:
    opened = 0

    async def count(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal opened
        opened += 1
        writer.close()

    server, port = await serve(count)
    reports = Reports()
    svc = ProbeService(ProbeSettings(), reports, node_id="me")
    try:
        command = msg(port)
        for key, value in bad.items():
            if key == "endpoint" and value.startswith("127.0.0.1"):
                value = value.replace("127.0.0.1", "127.0.0.1", 1)
            setattr(command, key, value)
        svc.handle(command)
        await asyncio.sleep(0.4)
    finally:
        await svc.close()
        server.close()
        await server.wait_closed()
    assert opened == 0
