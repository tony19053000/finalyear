"""V0.4 G4 tester round 1: workdir hygiene on a real agent (subprocess runtime).

D67: "Workdir deleted after the terminal status is acked unless keep_workdirs."
D63-2: the work root itself is owner-only (0o700). Neither is exercised
end-to-end anywhere in the implementer's suite (``test_docker_workdir_perms.py``
checks the *Docker* runtime's own chmod call directly, not the full executor
lifecycle through a real controller; nothing asserts the attempt directory is
actually gone from disk afterwards).
"""

from __future__ import annotations

import asyncio
import stat
import sys
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

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(60),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions"),
]

CODE = "open('marker.txt', 'w').write('x')\nprint('done')\n"
MANIFEST: dict[str, Any] = {"name": "g4", "entrypoint": ["python", "main.py"]}


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(pg_settings, jobs={"dispatch_interval_s": 0.1})


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, tmp_path: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, tmp_path / "agent-data")
    yield running
    await running.stop()


def _project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(CODE)
    return project


async def _wait_gone(path: Path, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(path.exists):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} still exists {within} s after the terminal report")


async def test_the_work_root_itself_is_owner_only(agent: ExecAgent) -> None:
    root = agent.executor.work_root
    assert root.exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


async def test_workdir_is_deleted_after_the_terminal_report_is_acked(
    http: httpx.AsyncClient, agent: ExecAgent, tmp_path: Path
) -> None:
    job_id = await submit(http, _project(tmp_path, "p1"), agent.node_id, dict(MANIFEST))
    job = await wait_job(http, job_id)
    assert job["state"] == "succeeded", job
    (attempt,) = job["attempts"]
    workdir = agent.executor.work_root / attempt["attempt_id"]
    await _wait_gone(workdir)


async def test_workdir_is_kept_with_keep_workdirs(
    grpc_target: str, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    keeping = await ExecAgent.start(
        grpc_target, tmp_path / "keep-data", execution={"keep_workdirs": True}
    )
    try:
        job_id = await submit(
            http, _project(tmp_path, "p2"), keeping.node_id, dict(MANIFEST)
        )
        job = await wait_job(http, job_id)
        assert job["state"] == "succeeded", job
        (attempt,) = job["attempts"]
        workdir = keeping.executor.work_root / attempt["attempt_id"]
        # give the (non-existent) cleanup task a moment it should not need
        await asyncio.sleep(0.5)
        assert workdir.exists() and (workdir / "marker.txt").exists()
    finally:
        await keeping.stop()
