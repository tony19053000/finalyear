"""V0.4 G5 D76: ``compute run / jobs / logs / cancel / get`` as a real process.

A real controller (PostgreSQL, gRPC, uvicorn) and a real agent run in this test's
event loop; ``compute`` runs as a **separate process** (``python -m
meshforge.cli``) with its configuration in the environment - the product surface
end to end, including the WebSocket follow and the exit codes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent, wait_job
from mf_fabric import ca_file
from mf_jobs_harness import token_of

from meshforge.common.exit_codes import ExitCode
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str


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


def _env(runtime: ControllerRuntime, data_dir: Path, tmp_path: Path) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": f"https://127.0.0.1:{runtime.http_port}",
            "MESHFORGE_CLI__CA_FILE": str(ca_file()),
            "MESHFORGE_CLI__TOKEN": token_of(runtime),
            "MESHFORGE_DATA_DIR": str(data_dir),  # the agent's: `--local` reads it
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
        }
    )
    return env


async def _compute(env: dict[str, str], *args: str, cwd: Path | None = None) -> Result:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meshforge.cli",
        *args,
        env=env,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 90)
    assert proc.returncode is not None
    return Result(proc.returncode, out.decode(), err.decode())


def _project(tmp_path: Path, code: str, manifest: str | None = None) -> Path:
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "main.py").write_text(code)
    if manifest is not None:
        (project / "compute.yaml").write_text(manifest)
    return project


async def test_run_follow_exit_code_is_the_job_outcome(
    runtime: ControllerRuntime, agent: ExecAgent, data_dir: Path, tmp_path: Path
) -> None:
    env = _env(runtime, data_dir, tmp_path)
    ok = _project(tmp_path / "ok", "print('hello from the fabric')\n")
    done = await _compute(env, "run", str(ok), "--node", agent.node_id, "-f")
    assert done.code == 0, done
    assert "hello from the fabric" in done.stdout
    assert done.stdout.splitlines()[0].startswith("job_")
    assert "idempotency key cli-" in done.stderr
    bad = _project(
        tmp_path / "bad", "import sys\nprint('oops', file=sys.stderr)\nsys.exit(3)\n"
    )
    failed = await _compute(env, "run", str(bad), "--local", "--follow")
    assert failed.code == 3, failed  # the workload's own exit code
    assert "oops" in failed.stderr and "user_error" in failed.stderr
    huge = _project(tmp_path / "huge", "import sys\nsys.exit(200)\n")
    clamped = await _compute(env, "run", str(huge), "--local", "-f")
    assert clamped.code == 125, clamped


async def test_run_follow_of_a_cancelled_job_exits_job_cancelled(
    runtime: ControllerRuntime,
    agent: ExecAgent,
    data_dir: Path,
    tmp_path: Path,
    http: httpx.AsyncClient,
) -> None:
    env = _env(runtime, data_dir, tmp_path)
    project = _project(
        tmp_path, "import time\nprint('ready', flush=True)\ntime.sleep(60)\n"
    )
    task = asyncio.create_task(_compute(env, "run", str(project), "--local", "-f"))
    job_id = ""
    for _ in range(200):
        listed = (await http.get("/v1/jobs")).json()["jobs"]
        running = [j for j in listed if j["state"] == "running"]
        if running:
            job_id = running[0]["job_id"]
            break
        await asyncio.sleep(0.05)
    assert job_id
    cancelled = await _compute(env, "cancel", job_id)
    assert cancelled.code == 0 and job_id in cancelled.stdout
    result = await task
    assert result.code == ExitCode.JOB_CANCELLED, result
    assert "ready" in result.stdout


async def test_get_logs_jobs_and_idempotent_resubmit(
    runtime: ControllerRuntime,
    agent: ExecAgent,
    data_dir: Path,
    tmp_path: Path,
    http: httpx.AsyncClient,
) -> None:
    env = _env(runtime, data_dir, tmp_path)
    code = (
        "import os, sys\nos.makedirs('out', exist_ok=True)\n"
        "open('out/r.txt', 'w').write('result 42\\n')\n"
        "print('to stdout')\nprint('to stderr', file=sys.stderr)\n"
    )
    manifest = "name: cli-demo\nentrypoint: python main.py\noutputs: [out/, nothing/]\n"
    project = _project(tmp_path, code, manifest)
    args = ("run", str(project), "--node", agent.node_id, "--idempotency-key", "k-123")
    first = await _compute(env, *args)
    again = await _compute(env, *args)
    assert first.code == again.code == 0
    job_id = first.stdout.strip()
    assert again.stdout.strip() == job_id  # the same key: the same job
    await wait_job(http, job_id)
    listed = await _compute(env, "jobs", "--json")
    assert job_id in [j["job_id"] for j in json.loads(listed.stdout)["jobs"]]
    logs = await _compute(env, "logs", job_id)
    assert logs.stdout == "to stdout\n" and logs.stderr == "to stderr\n"
    dest = tmp_path / "got"
    got = await _compute(env, "get", job_id, "--dest", str(dest))
    assert got.code == 0, got
    assert (dest / "out" / "r.txt").read_text() == "result 42\n"
    assert "nothing/" in got.stderr  # missing outputs are listed
    refused = await _compute(env, "get", job_id, "--dest", str(dest))
    assert refused.code == ExitCode.FAILURE and "--force" in refused.stderr
    forced = await _compute(env, "get", job_id, "--dest", str(dest), "--force")
    assert forced.code == 0
    default = await _compute(env, "get", job_id, cwd=tmp_path)
    assert (tmp_path / "outputs" / job_id / "out" / "r.txt").exists(), default


async def test_usage_errors(
    runtime: ControllerRuntime, agent: ExecAgent, data_dir: Path, tmp_path: Path
) -> None:
    env = _env(runtime, data_dir, tmp_path)
    project = _project(tmp_path, "print(1)\n")
    both = await _compute(env, "run", str(project), "--local", "--node", agent.node_id)
    assert both.code == ExitCode.USAGE_ERROR
    # V0.6 D103: neither flag is no longer a usage error - the planner places
    # the job automatically (this laptop's agent is its Local candidate).
    neither = await _compute(env, "run", str(project))
    assert neither.code == ExitCode.OK, neither.stderr
    assert neither.stdout.strip().startswith("job_")
    bad_policy = await _compute(env, "run", str(project), "--policy", "fastest")
    assert bad_policy.code == ExitCode.USAGE_ERROR
    bad_id = await _compute(env, "logs", "../healthz")
    assert bad_id.code == ExitCode.USAGE_ERROR
    no_agent = dict(env, MESHFORGE_DATA_DIR=str(tmp_path / "empty"))
    local = await _compute(no_agent, "run", str(project), "--local")
    assert local.code == ExitCode.CONFIG_ERROR and "--node" in local.stderr
