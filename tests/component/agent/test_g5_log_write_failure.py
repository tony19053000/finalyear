"""V0.4 G5 fix round (reviewer B1): a live-log write failure never stops the controller.

Real PostgreSQL + gRPC + REST + a real agent (subprocess runtime). A symlink is
planted at a running attempt's live stdout path; the controller opens it with
``O_NOFOLLOW`` (``ELOOP``). Before the fix that ``OSError`` escaped the LogHub
writer task and ``ControllerRuntime._on_task_done`` stopped the controller. Now:
the symlink target is never written, the lost range is a recorded gap, the job
still succeeds (its verified final log replaces the link) and a second job on
the same controller runs to completion.
"""

from __future__ import annotations

import asyncio
import os
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
    pytest.mark.timeout(180),
    pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW"),
]

LATE_TALKER = """\
import time
time.sleep(2.0)
for i in range(20):
    print(f"line {i:04d}", flush=True)
    time.sleep(0.05)
"""

QUICK = 'print("second job ok")\n'


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(pg_settings, jobs={"dispatch_interval_s": 0.1})


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


def _project(tmp_path: Path, name: str, code: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(code)
    return project


async def test_a_planted_symlink_at_the_log_path_does_not_stop_the_controller(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    hub = runtime.logs
    assert hub is not None
    manifest = {"name": "late-talker", "entrypoint": ["python", "main.py"]}
    job_id = await submit(
        http, _project(tmp_path, "a", LATE_TALKER), agent.node_id, manifest
    )
    job = await wait_job(http, job_id, states=frozenset({"running"}))
    attempt_id = job["attempts"][0]["attempt_id"]
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    live = hub.path(attempt_id, "stdout")
    assert not live.exists(), "the workload printed before the link was planted"
    live.symlink_to(victim)

    job = await wait_job(http, job_id, timeout_s=60)
    assert job["state"] == "succeeded", job
    assert runtime.failed_task is None
    assert hub.stats.write_failures >= 1
    assert victim.read_bytes() == b"precious"  # never followed
    for _ in range(100):  # the final copy runs right after the terminal status
        state = hub.ended_state(attempt_id)
        if state is not None and state.final.get("stdout"):
            break
        await asyncio.sleep(0.05)
    state = hub.ended_state(attempt_id)
    assert state is not None
    assert state.gaps().get("stdout"), "the lost live range must be a recorded gap"
    # the verified final log replaced the planted link (os.replace on the name)
    assert not live.is_symlink()
    assert b"line 0019" in live.read_bytes()

    # the controller keeps serving: a second job runs to completion
    second = await submit(
        http,
        _project(tmp_path, "b", QUICK),
        agent.node_id,
        {"name": "second", "entrypoint": ["python", "main.py"]},
    )
    done = await wait_job(http, second, timeout_s=60)
    assert done["state"] == "succeeded", done
    assert runtime.failed_task is None
    second_attempt = done["attempts"][0]["attempt_id"]
    for _ in range(100):
        ended = hub.ended_state(second_attempt)
        if ended is not None and ended.final.get("stdout"):
            break
        await asyncio.sleep(0.05)
    assert b"second job ok" in hub.path(second_attempt, "stdout").read_bytes()
