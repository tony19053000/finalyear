"""V0.4 G5 D72 end to end: ``GET /v1/jobs/{id}/logs`` and the WebSocket follow.

Real PostgreSQL + gRPC + uvicorn + a real agent (subprocess runtime) + a real
``websockets`` client over TCP.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from meshforge.controller.api import log_routes
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

TALKER = """\
import sys, time
for i in range(20):
    print(f"out {i}", flush=True)
    print(f"err {i}", file=sys.stderr, flush=True)
    time.sleep(0.05)
"""

FLOOD = """\
import sys
block = b"L" * (1024 * 1024)
for _ in range(48):
    sys.stdout.buffer.write(block)
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={
            "dispatch_interval_s": 0.1,
            "log_subscriber_queue": 8,
            "max_log_subscribers": 2,
        },
        **tolerant_liveness(),  # not a liveness test (see TOLERANT_LIVENESS)
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


def _project(tmp_path: Path, code: str) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(code)
    return project


def _ws_url(runtime: ControllerRuntime, job_id: str, query: str = "") -> str:
    return f"ws://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws{query}"


def _auth(runtime: ControllerRuntime) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_of(runtime)}"}


async def _follow(
    url: str, headers: dict[str, str]
) -> tuple[dict[str, bytes], dict[str, Any]]:
    got = {"stdout": b"", "stderr": b""}
    async with connect(url, additional_headers=headers) as ws:
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("end"):
                return got, frame
            assert frame["offset"] == len(got[frame["stream"]])  # contiguous
            got[frame["stream"]] += base64.b64decode(frame["data"])
    raise AssertionError("closed without an end frame")


async def test_follow_from_submit_to_end_equals_the_persisted_log(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A follower's idle wait for a hub event (1 s in production) made huge: a
    # follower that ever falls back to it (instead of the hub's end event, or
    # the already-ended check for a late joiner) blows the wait_for bounds
    # below. Deterministic, unlike the wall-clock "< 0.9 s" this replaced,
    # which a loaded machine could miss with no wait taken at all.
    monkeypatch.setattr(log_routes, "_LIVE_WAIT_S", 3600.0)
    manifest = {"name": "talk", "entrypoint": ["python", "main.py"]}
    job_id = await submit(http, _project(tmp_path, TALKER), agent.node_id, manifest)
    got, end = await asyncio.wait_for(
        _follow(_ws_url(runtime, job_id), _auth(runtime)), 30
    )
    assert end["state"] == "succeeded" and end["exit_code"] == 0
    assert end["final"] == {"stdout": True, "stderr": True}
    assert got["stdout"] == b"".join(f"out {i}\n".encode() for i in range(20))
    assert got["stderr"] == b"".join(f"err {i}\n".encode() for i in range(20))
    plain = await http.get(f"/v1/jobs/{job_id}/logs")
    split = int(plain.headers["x-meshforge-stdout-bytes"])
    assert plain.content[:split] == got["stdout"]
    assert plain.content[split:] == got["stderr"]
    only_err = await http.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stderr"})
    assert only_err.content == got["stderr"]
    # a follower joining after the end gets everything and the end frame at
    # once - no idle wait (it would be _LIVE_WAIT_S = 1 h here)
    again, end2 = await asyncio.wait_for(
        _follow(_ws_url(runtime, job_id), _auth(runtime)), 30
    )
    assert again == got and end2["state"] == "succeeded"


async def test_resume_from_an_offset(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    manifest = {"name": "talk", "entrypoint": ["python", "main.py"]}
    job_id = await submit(http, _project(tmp_path, TALKER), agent.node_id, manifest)
    await wait_job(http, job_id)
    full = (
        await http.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
    ).content
    tail = b""
    async with connect(
        _ws_url(runtime, job_id, "?stream=stdout&stdout_offset=6"),
        additional_headers=_auth(runtime),
    ) as ws:
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("end"):
                break
            tail += base64.b64decode(frame["data"])
    assert tail == full[6:]


async def test_the_websocket_needs_the_token_in_the_header(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    manifest = {"name": "t", "entrypoint": ["python", "main.py"]}
    job_id = await submit(http, _project(tmp_path, TALKER), agent.node_id, manifest)
    with pytest.raises(InvalidStatus) as info:
        async with connect(_ws_url(runtime, job_id)):
            pass
    assert info.value.response.status_code == 403
    token = token_of(runtime)
    with pytest.raises(
        InvalidStatus
    ):  # a token in the query string is not a credential
        async with connect(_ws_url(runtime, job_id, f"?token={token}")):
            pass
    async with httpx.AsyncClient() as anonymous:
        plain = await anonymous.get(
            f"http://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs"
        )
    assert plain.status_code == 401


async def test_unknown_job_and_follower_limit_close_typed(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    async with connect(
        _ws_url(runtime, "job_01M3M8MZR0FQ9KR9PDH724CGPP"),
        additional_headers=_auth(runtime),
    ) as ws:
        with pytest.raises(ConnectionClosed):
            await ws.recv()
    assert ws.close_code == log_routes.CLOSE_NOT_FOUND
    project = _project(tmp_path, "import time\ntime.sleep(3)\n")
    job_id = await submit(
        http, project, agent.node_id, {"name": "s", "entrypoint": ["python", "main.py"]}
    )
    await wait_job(http, job_id, states=frozenset({"running"}))
    url = _ws_url(runtime, job_id)
    async with (
        connect(url, additional_headers=_auth(runtime)),
        connect(url, additional_headers=_auth(runtime)),
    ):
        await asyncio.sleep(0.3)  # both subscribed (max_log_subscribers = 2)
        async with connect(url, additional_headers=_auth(runtime)) as third:
            with pytest.raises(ConnectionClosed):
                await third.recv()
        assert third.close_code == log_routes.CLOSE_TOO_MANY
    await http.post(f"/v1/jobs/{job_id}/cancel")


def _raw_ws(port: int, path: str, token: str) -> socket.socket:
    """A WebSocket client that truly stops reading: a raw socket with a tiny
    receive buffer (library clients keep draining the socket into memory)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n\r\n"
        ).encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(1)
    assert head.startswith(b"HTTP/1.1 101"), head
    return sock


def _close_code(sock: socket.socket) -> int | None:
    """Read server frames until the close frame; its status code."""
    sock.settimeout(30)
    buf = b""

    def need(n: int) -> bytes:
        nonlocal buf
        while len(buf) < n:
            more = sock.recv(1 << 20)
            if not more:
                raise EOFError
            buf += more
        out, buf = buf[:n], buf[n:]
        return out

    while True:
        b0, b1 = need(2)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", need(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", need(8))[0]
        payload = need(length)
        if b0 & 0x0F == 0x8:
            return struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None


async def test_a_slow_follower_is_disconnected_and_the_job_finishes(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(log_routes, "_SEND_TIMEOUT_S", 0.5)
    project = _project(tmp_path, "import time\ntime.sleep(1.0)\n" + FLOOD)
    manifest = {"name": "flood", "entrypoint": ["python", "main.py"]}
    job_id = await submit(http, project, agent.node_id, manifest)
    sock = await asyncio.to_thread(
        _raw_ws, runtime.http_port, f"/v1/jobs/{job_id}/logs/ws", token_of(runtime)
    )
    try:
        job = await wait_job(http, job_id, timeout_s=60)  # sock is never read
        assert job["state"] == "succeeded", job  # the job never waited for it
        code = await asyncio.to_thread(_close_code, sock)
    finally:
        sock.close()
    assert code == log_routes.CLOSE_SLOW_CONSUMER
    assert runtime.logs is not None and runtime.logs.stats.slow_subscribers == 1
