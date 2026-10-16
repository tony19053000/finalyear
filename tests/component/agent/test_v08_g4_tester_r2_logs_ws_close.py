"""V0.8 G4 tester r2: the logs WebSocket when the browser closes first (Lead fix 1d31ade, D196).

The dashboard's follower closes its socket the moment it reads the end frame. The controller must
not log "Exception in ASGI application" / a WebSocketDisconnect traceback however the client
leaves: clean close right after the end frame, hard abort (no close handshake) right after it, a
leave in the middle of the stream, and a leave while the job is still running. Real PostgreSQL +
gRPC + uvicorn + a real agent + a real ``websockets`` client.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from websockets.exceptions import ConnectionClosed

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

TALKER = """\
import sys, time
for i in range(30):
    print(f"out {i}", flush=True)
    print(f"err {i}", file=sys.stderr, flush=True)
    time.sleep(0.02)
"""
SLOW = (
    "import time\nfor i in range(40):\n    print(i, flush=True)\n    time.sleep(0.1)\n"
)


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "max_log_subscribers": 64},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


def _project(tmp_path: Path, name: str, code: str) -> Path:
    project = tmp_path / name
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(code)
    return project


def _url(runtime: ControllerRuntime, job_id: str) -> str:
    return f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"


def _bad(caplog: pytest.LogCaptureFixture) -> list[str]:
    out = []
    for r in caplog.records:
        text = r.getMessage()
        if "Exception in ASGI application" in text or "WebSocketDisconnect" in text:
            out.append(f"{r.name}: {text[:200]}")
        elif r.exc_info and r.levelno >= logging.ERROR:
            out.append(f"{r.name}: {text[:120]} exc={r.exc_info[0]}")
    return out


async def _leave_at_end(url: str, headers: dict[str, str], how: str) -> None:
    ws = await connect(url, additional_headers=headers)
    try:
        async for raw in ws:
            if json.loads(raw).get("end"):
                break
    except ConnectionClosed:
        pass
    if how == "close":
        await ws.close()
    elif how == "abort":
        ws.transport.abort()  # no close handshake: a browser tab killed
    # how == "drop": leave the object to be garbage-collected after the loop yields


async def _leave_midway(url: str, headers: dict[str, str], how: str) -> None:
    ws = await connect(url, additional_headers=headers)
    await ws.recv()
    if how == "abort":
        ws.transport.abort()
    else:
        await ws.close()


async def test_client_closing_first_never_logs_a_server_traceback(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    headers = {"Authorization": f"Bearer {token_of(runtime)}"}
    done = await submit(
        http,
        _project(tmp_path, "t", TALKER),
        agent.node_id,
        {"name": "talk", "entrypoint": ["python", "main.py"]},
    )
    await wait_job(http, done)
    url = _url(runtime, done)

    # 1. finished job, end frame at once, then leave in every way, many times (the race window
    #    is a few ms: repetition + concurrency is what makes it bite)
    for round_ in range(12):
        batch = [
            _leave_at_end(url, headers, how)
            for how in ("close", "abort", "close", "abort", "drop") * 4
        ]
        await asyncio.wait_for(asyncio.gather(*batch), 60)
        assert round_ >= 0
    # 2. leave mid-stream of the finished job
    await asyncio.gather(
        *[_leave_midway(url, headers, h) for h in ("abort", "close") * 6]
    )

    # 3. a running job: followers leave at its end frame (the live path) and mid-run
    live = await submit(
        http,
        _project(tmp_path, "s", SLOW),
        agent.node_id,
        {"name": "slow", "entrypoint": ["python", "main.py"]},
    )
    await wait_job(http, live, states=frozenset({"running", "succeeded"}))
    live_url = _url(runtime, live)
    mid = [_leave_midway(live_url, headers, h) for h in ("abort", "close", "abort")]
    at_end = [_leave_at_end(live_url, headers, h) for h in ("close", "abort", "close")]
    await asyncio.wait_for(asyncio.gather(*mid, *at_end), 60)
    await wait_job(http, live)

    await asyncio.sleep(1.0)  # let the server finish handling the last disconnects
    assert _bad(caplog) == []
    # the controller still serves a normal follower afterwards
    await _leave_at_end(url, headers, "close")
    assert (await http.get(f"/v1/jobs/{done}/logs")).status_code == 200


async def test_close_race_made_deterministic_client_leaves_before_the_server_closes(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widen the window: the final server ``close`` waits 150 ms; the client leaves at the end.

    Without the D196 suppression the late ``close`` raises WebSocketDisconnect out of the route and
    uvicorn logs "Exception in ASGI application" (the intermittent teardown error of tester r1).
    """
    import starlette.websockets as sw

    caplog.set_level(logging.WARNING)
    headers = {"Authorization": f"Bearer {token_of(runtime)}"}
    job = await submit(
        http,
        _project(tmp_path, "r", TALKER),
        agent.node_id,
        {"name": "talk", "entrypoint": ["python", "main.py"]},
    )
    await wait_job(http, job)
    real_close = sw.WebSocket.close
    closes = 0

    async def slow_close(
        self: Any, code: int = 1000, reason: str | None = None
    ) -> None:
        nonlocal closes
        closes += 1
        await asyncio.sleep(0.15)
        await real_close(self, code, reason)

    monkeypatch.setattr(sw.WebSocket, "close", slow_close)
    url = _url(runtime, job)
    await asyncio.gather(
        *[_leave_at_end(url, headers, h) for h in ("abort", "close") * 3]
    )
    await asyncio.sleep(0.8)
    monkeypatch.undo()
    assert closes >= 6  # the window really was exercised
    assert _bad(caplog) == []
