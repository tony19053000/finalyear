"""V0.4 G5: ``lab/exec_check.py`` (the 0.4.15 measurement tool) against a real
controller + agent, run as a process exactly as the checklist runs it."""

from __future__ import annotations

import asyncio
import json
import os
import sys
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

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(pg_settings, jobs={"dispatch_interval_s": 0.1})


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as client:
        yield client


async def _tool(runtime: ControllerRuntime, cwd: Path, *args: str) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["MESHFORGE_CLI__TOKEN"] = token_of(runtime)
    env["XDG_CONFIG_HOME"] = str(cwd / "cfg")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(REPO / "lab" / "exec_check.py"),
        "--controller",
        f"https://127.0.0.1:{runtime.http_port}",
        "--reports",
        str(cwd / "reports"),
        *args,
        env=env,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 90)
    assert proc.returncode is not None
    return proc.returncode, out.decode()


async def test_latency_and_cancel_measurements(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    ticker = REPO / "lab" / "workloads" / "log-ticker"
    manifest = {"name": "ticker", "entrypoint": ["python", "main.py", "55", "2"]}
    job_id = await submit(http, ticker, agent.node_id, manifest)
    code, out = await _tool(runtime, tmp_path, "latency", job_id, "--name", "L1")
    assert code == 0, out
    result = json.loads(out[out.index("{") :])
    assert result["lines"] == 55 and result["within"] is True
    assert 0 <= result["min_ms"] <= result["p50_ms"] <= result["max_ms"] <= 1000
    sleeper = tmp_path / "sleeper"
    sleeper.mkdir()
    (sleeper / "main.py").write_text("import time\ntime.sleep(60)\n")
    job2 = await submit(
        http, sleeper, agent.node_id, {"name": "s", "entrypoint": ["python", "main.py"]}
    )
    await wait_job(http, job2, states=frozenset({"running"}))
    code, out = await _tool(runtime, tmp_path, "cancel", job2, "--name", "L1")
    assert code == 0, out
    assert json.loads(out[out.index("{") :])["state"] == "cancelled"
    lines = next((tmp_path / "reports").glob("*-V0.4-exec-check.jsonl")).read_text()
    assert [json.loads(x)["check"] for x in lines.splitlines()] == ["latency", "cancel"]
