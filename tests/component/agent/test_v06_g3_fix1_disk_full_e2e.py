"""V0.6 G3 fix round 1 (D126) end to end: real controller, real agents (client +
executor in this loop), real PostgreSQL, the real planner.

Agent A's disk is "full": its staging step raises ``OSError(ENOSPC)`` (the
executor types that ``disk_full``, D116 - the real-ENOSPC version on a tmpfs is
the cluster test ``test_v06_g3_disk_full_node``).

* an **auto-placed** job that hits it on A is retried in the same transaction,
  A is excluded (``retry_excluded`` in attempt 2's decision) and the retry
  succeeds on B;
* a job **pinned** to A fails ``disk_full`` after one attempt.

To make A the first choice the job is submitted while only A is connected,
with a backoff long enough for B to connect before the retry is placed.
"""

from __future__ import annotations

import asyncio
import errno
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

WORKLOAD = "print('ran', flush=True)\n"


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
async def started(grpc_target: str, tmp_path: Path) -> AsyncIterator[list[ExecAgent]]:
    agents: list[ExecAgent] = []
    yield agents
    for agent in agents:
        await agent.stop()


def _full_disk(agent: ExecAgent) -> list[str]:
    """Every staging on ``agent`` hits ENOSPC; returns the attempt ids it hit."""
    hit: list[str] = []

    async def stage(attempt: Any, *_args: Any) -> None:
        hit.append(attempt.attempt_id)
        raise OSError(errno.ENOSPC, "No space left on device")

    agent.executor._stage = stage  # type: ignore[method-assign]
    return hit


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(WORKLOAD)
    return project


MANIFEST = {"name": "g3-disk", "entrypoint": ["python", "main.py"]}


async def test_disk_full_on_a_retries_on_b_and_succeeds(
    http: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    started: list[ExecAgent],
    runtime: ControllerRuntime,
) -> None:
    a = await ExecAgent.start(grpc_target, tmp_path / "a")
    started.append(a)
    hit = _full_disk(a)
    job_id = await submit(
        http, _project(tmp_path), None, {**MANIFEST, "retry": {"backoff_s": 4}}
    )
    for _ in range(600):  # the first attempt ran (and failed) on A
        if hit:
            break
        await asyncio.sleep(0.05)
    job = await wait_job(http, job_id, states=frozenset({"queued"}), timeout_s=30)
    assert [x["state"] for x in job["attempts"]] == ["failed"], job  # retry waits
    b = await ExecAgent.start(grpc_target, tmp_path / "b")
    started.append(b)
    job = await wait_job(http, job_id, timeout_s=30)
    assert job["state"] == "succeeded", job
    first, second = job["attempts"]
    assert (first["node_id"], first["state"], first["failure_type"]) == (
        a.node_id,
        "failed",
        "disk_full",
    )
    assert (second["node_id"], second["state"]) == (b.node_id, "done")
    assert hit == [first["attempt_id"]]  # A was never tried again
    resp = await http.get(f"/v1/jobs/{job_id}/decisions")
    assert resp.status_code == 200, resp.text
    (retry,) = [
        d
        for d in resp.json()["decisions"]
        if d["outcome"] == "dispatched" and d["attempt_no"] == 2
    ]
    assert retry["chosen_node_id"] == b.node_id
    a_row = next(c for c in retry["candidates"] if c["node_id"] == a.node_id)
    assert a_row["reject_code"] == "retry_excluded", retry
    assert runtime.dispatcher is not None
    assert runtime.dispatcher.stats.retries_scheduled >= 1


async def test_disk_full_of_a_job_pinned_to_a_fails_after_one_attempt(
    http: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    started: list[ExecAgent],
) -> None:
    a = await ExecAgent.start(grpc_target, tmp_path / "a")
    b = await ExecAgent.start(grpc_target, tmp_path / "b")
    started.extend([a, b])
    hit = _full_disk(a)
    job_id = await submit(
        http, _project(tmp_path), a.node_id, {**MANIFEST, "retry": {"backoff_s": 0}}
    )
    job = await wait_job(http, job_id, timeout_s=30)
    assert (job["state"], job["failure_type"]) == ("failed", "disk_full"), job
    (only,) = job["attempts"]
    assert only["node_id"] == a.node_id
    assert hit == [only["attempt_id"]]
    assert "retrying" not in [t["to_state"] for t in job["transitions"]]
