"""V0.4 G5 D71 end to end: live logs, final logs, floods.

Real PostgreSQL + gRPC + REST + a real agent (subprocess runtime).

* A job prints for a while; its session is cut mid-run (the agent reconnects).
  When it ends, the controller's log file is **byte-equal** to the agent's local
  log (the final upload replaced the live copy, whatever the reconnect dropped),
  and live chunks did arrive while it ran.
* A log flood (~200 MB/s) does not delay the acks of reports sent meanwhile
  beyond a stated bound (1 s on loopback), and the log is truncated at the cap
  with the marker line - never silently.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.agent.execution.logs import truncation_marker
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

TALKER = """\
import sys, time
for i in range(40):
    print(f"line {i:04d} " + "x" * 200, flush=True)
    print(f"err {i}", file=sys.stderr, flush=True)
    time.sleep(0.05)
"""

FLOOD = """\
import sys, time
block = b"F" * (1024 * 1024)
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    sys.stdout.buffer.write(block)
sys.stdout.buffer.flush()
"""

CAP = 64 * 1024 * 1024  # the default per-stream cap


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(pg_settings, jobs={"dispatch_interval_s": 0.1})


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
    running = await ExecAgent.start(
        grpc_target,
        data_dir,
        execution={"keep_workdirs": True},
    )
    yield running
    await running.stop()


def _project(tmp_path: Path, code: str) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(code)
    return project


async def test_logs_are_complete_after_a_mid_run_reconnect(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    manager, hub = runtime.manager, runtime.logs
    assert manager is not None and hub is not None
    manifest = {"name": "talker", "entrypoint": ["python", "main.py"]}
    job_id = await submit(http, _project(tmp_path, TALKER), agent.node_id, manifest)
    await wait_job(http, job_id, states=frozenset({"running"}))
    await asyncio.sleep(0.6)
    session = manager.get(agent.node_id)
    assert session is not None
    await manager.end_session(session, SessionEndReason.STREAM_LOST)
    job = await wait_job(http, job_id, timeout_s=30)
    assert job["state"] == "succeeded", job
    attempt_id = job["attempts"][0]["attempt_id"]
    local = agent.executor.work_root / attempt_id / ".meshforge"
    for stream in ("stdout", "stderr"):
        for _ in range(100):  # the final copy runs right after the terminal status
            state = hub.ended_state(attempt_id)
            if state is not None and state.final.get(stream):
                break
            await asyncio.sleep(0.05)
        ours = hub.path(attempt_id, stream).read_bytes()
        assert ours == (local / f"{stream}.log").read_bytes(), stream
    assert hub.stats.chunks_appended > 0  # the live path carried data meanwhile
    assert b"line 0039" in hub.path(attempt_id, "stdout").read_bytes()


async def test_a_log_flood_does_not_delay_report_acks_and_is_truncated(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    hub = runtime.logs
    assert hub is not None
    manifest = {"name": "flood", "entrypoint": ["python", "main.py", "2.0"]}
    job_id = await submit(http, _project(tmp_path, FLOOD), agent.node_id, manifest)
    await wait_job(http, job_id, states=frozenset({"running"}))
    client = agent.client
    delays: list[float] = []
    for _ in range(15):
        sent = time.monotonic()
        mid = await client.send_report(
            "resource_update", agent_pb2.ResourceUpdate(cpu_percent=1.0)
        )
        for _ in range(2000):  # polling an ack: no event to wait on here
            if client.ring.get(mid) is None:
                break
            await asyncio.sleep(0.005)
        delays.append(time.monotonic() - sent)
        await asyncio.sleep(0.1)
    job = await wait_job(http, job_id, timeout_s=60)
    assert job["state"] == "succeeded", job
    assert max(delays) <= 1.0, delays  # the stated bound (loopback)
    attempt_id = job["attempts"][0]["attempt_id"]
    for _ in range(100):
        state = hub.ended_state(attempt_id)
        if state is not None and state.final.get("stdout"):
            break
        await asyncio.sleep(0.05)
    data = hub.path(attempt_id, "stdout").read_bytes()
    assert len(data) == CAP + len(truncation_marker(CAP))
    assert data.endswith(truncation_marker(CAP))
    detail = (await http.get(f"/v1/jobs/{job_id}")).json()
    assert detail["attempts"][0]["logs"]["stdout"]["truncated"] is True
    print(  # noqa: T201 - measured, for the handoff
        "Laptop 1 (not a benchmark claim): report ack under a log flood "
        f"max {max(delays) * 1000:.0f} ms, dropped live chunks "
        f"{client.stats.log_chunks_dropped}"
    )
