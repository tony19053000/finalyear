"""V0.6 G2 (0.6.11, D103, D109) end to end: ``compute run`` on a laptop with an
enrolled agent seeds that agent's cache and the planner runs the job **Local**.

A real controller and two real agents (subprocess runtime) in this loop; one
of them ("local") uses the data dir the ``compute`` process reads, so it is
the submitter's own node. ``compute run`` (a separate process) uploads a
project with a 3 MiB file, copies it into the local agent's cache and submits
``submitter_node_id`` + ``seeded``. Under ``cost_model`` the Local candidate
has nothing to download (0 s, ``measured``) and the remote one an assumed
download - so the job runs on the laptop that submitted it, its staging is all
cache hits, and ``compute explain`` names the Local mode. With seeding turned
off the job still runs (the agent downloads); the decision then has no free
advantage for Local.
"""

from __future__ import annotations

import asyncio
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
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, wait_job
from mf_jobs_harness import token_of

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

BIG = 3 * 1024 * 1024


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.05},
        planner={"default_policy": "cost_model"},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def agents(
    grpc_target: str, tmp_path: Path
) -> AsyncIterator[dict[str, ExecAgent]]:
    running = {
        "remote": await ExecAgent.start(grpc_target, tmp_path / "remote-data"),
        "local": await ExecAgent.start(grpc_target, tmp_path / "local-data"),
    }
    yield running
    for agent in running.values():
        await agent.stop()


def _env(
    runtime: ControllerRuntime, data_dir: Path, tmp_path: Path, **extra: str
) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": f"http://127.0.0.1:{runtime.http_port}",
            "MESHFORGE_CLI__TOKEN": token_of(runtime),
            "MESHFORGE_DATA_DIR": str(data_dir),
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
            "COLUMNS": "250",
            **extra,
        }
    )
    return env


async def _compute(env: dict[str, str], *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meshforge.cli",
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 90)
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


def _project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text("print('tiny job', flush=True)\n")
    (project / "weights.bin").write_bytes(os.urandom(BIG))
    return project


async def test_seeded_local_runs_on_the_submitters_own_agent(
    runtime: ControllerRuntime, agents: dict[str, ExecAgent], tmp_path: Path
) -> None:
    local = agents["local"]
    env = _env(runtime, tmp_path / "local-data", tmp_path)
    code, out, err = await _compute(env, "run", str(_project(tmp_path, "p1")))
    assert code == 0, err
    job_id = out.strip()
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as http:
        job = await wait_job(http, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        assert job["placement"]["mode"] == "auto"
        assert job["submitter_node_id"] == local.node_id
        (attempt,) = job["attempts"]
        assert attempt["node_id"] == local.node_id
        # staging was all cache hits: nothing came from the controller
        sources = {row["source"]: row for row in attempt["transfers"]}
        assert sources["cache"]["bytes"] >= BIG
        assert sources.get("controller", {"bytes": 0})["bytes"] == 0
        decisions = (await http.get(f"/v1/jobs/{job_id}/decisions")).json()
    (decision,) = decisions["decisions"]
    chosen = next(c for c in decision["candidates"] if c["rank"] == 1)
    assert chosen["node_id"] == local.node_id and chosen["mode"] == "local"
    transfer = chosen["estimate"]["terms"]["transfer"]
    assert transfer == {
        "seconds": 0.0,
        "source": "measured",
        "detail": {"missing_bytes": 0},
    }
    remote = next(c for c in decision["candidates"] if c["node_id"] != local.node_id)
    assert remote["mode"] == "offload"
    assert remote["estimate"]["terms"]["transfer"]["source"] == "assumed"
    assert remote["estimate"]["terms"]["transfer"]["detail"]["missing_bytes"] >= BIG
    code, printed, err = await _compute(env, "explain", job_id)
    assert code == 0, err
    assert "local" in printed and "0.00s (measured)" in printed


async def test_without_seeding_the_job_still_runs_and_local_has_no_free_ride(
    runtime: ControllerRuntime, agents: dict[str, ExecAgent], tmp_path: Path
) -> None:
    env = _env(
        runtime,
        tmp_path / "local-data",
        tmp_path,
        MESHFORGE_CLI__SEED_LOCAL="false",
    )
    code, out, err = await _compute(env, "run", str(_project(tmp_path, "p2")))
    assert code == 0, err
    job_id = out.strip()
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as http:
        job = await wait_job(http, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        decisions = (await http.get(f"/v1/jobs/{job_id}/decisions")).json()
    (decision,) = decisions["decisions"]
    for candidate in decision["candidates"]:
        terms = candidate["estimate"]["terms"]
        assert terms["transfer"]["detail"]["missing_bytes"] >= BIG, candidate
    assert agents["local"].node_id in {c["node_id"] for c in decision["candidates"]}
