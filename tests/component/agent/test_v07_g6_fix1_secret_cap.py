"""V0.7 G6 fix round 1 (D162, T-8): the per-job total-secret-bytes cap.

Real mTLS controller, real PostgreSQL, a real agent in this loop.
``[controller.jobs] max_job_secret_bytes`` (default 256 KiB) bounds what the
log redactor has to search for:

* **submit**: a manifest whose secrets together exceed it is refused 400
  ``invalid_input`` ``secret_bytes_over_limit`` (no job row); exactly at the
  cap is accepted and runs;
* **dispatch**: a value replaced by a larger one while the job waits fails the
  attempt ``FAILED(invalid_input)`` ``secret_bytes_over_limit`` before anything
  reaches the node - one attempt, never retried, never hangs;
* the default is 256 KiB and four 64 KiB secrets fit it, a fifth does not.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings, JobApiSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

CAP = 100
WORKLOAD = "import sys\nopen(sys.argv[1], 'w').write('ran')\n"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 0.1, "max_job_secret_bytes": CAP},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def alice(
    engine: AsyncEngine, runtime: ControllerRuntime
) -> AsyncIterator[httpx.AsyncClient]:
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        await create_user(engine, "alice", "alice-password-1")
        token = await login(anon, "alice", "alice-password-1")
    async with httpx.AsyncClient(
        base_url=base, headers=bearer(token), timeout=30
    ) as client:
        yield client


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(WORKLOAD)
    return project


def _manifest(tmp_path: Path, secrets: list[Any]) -> dict[str, Any]:
    return {
        "name": "g6-cap",
        "entrypoint": ["python", "main.py", str(tmp_path / "ran.txt")],
        "secrets": secrets,
    }


async def _agent(grpc_target: str, data_dir: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )


async def test_the_default_cap_is_256_kib() -> None:
    assert JobApiSettings().max_job_secret_bytes == 256 * 1024


async def test_submit_refuses_a_job_over_the_cap_and_accepts_one_at_it(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    half, rest = os.urandom(CAP // 2), os.urandom(CAP // 2)
    for name, value in (("A", half), ("B", rest), ("C", b"four")):
        assert (await alice.put(f"/v1/secrets/{name}", content=value)).is_success
    agent = await _agent(grpc_target, tmp_path / "a")
    try:
        with pytest.raises(AssertionError) as exc:
            await submit(
                alice, _project(tmp_path), None, _manifest(tmp_path, ["A", "B", "C"])
            )
        text = str(exc.value)
        assert "secret_bytes_over_limit" in text and "invalid_input" in text
        assert f"total {CAP + 4} bytes" in text and f"at most {CAP} " in text
        assert "max_job_secret_bytes" in text
        assert (await alice.get("/v1/jobs")).json().get("jobs", []) == []
        assert not (tmp_path / "ran.txt").exists()
        # Exactly at the cap: accepted, and it runs.
        job_id = await submit(
            alice, _project(tmp_path), None, _manifest(tmp_path, ["A", "B"])
        )
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        assert (tmp_path / "ran.txt").read_text() == "ran"
    finally:
        await agent.stop()


async def test_a_value_grown_while_queued_fails_the_job_before_the_node(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    assert (await alice.put("/v1/secrets/GROWS", content=b"small-value")).is_success
    first = await _agent(grpc_target, tmp_path / "a")
    await first.stop()  # enrolled, offline: the job waits in the queue
    job_id = await submit(
        alice, _project(tmp_path), None, _manifest(tmp_path, ["GROWS"])
    )
    grown = os.urandom(CAP + 1)
    assert (await alice.put("/v1/secrets/GROWS", content=grown)).is_success
    again = await _agent(grpc_target, tmp_path / "a")
    try:
        job = await wait_job(alice, job_id, timeout_s=30)
    finally:
        await again.stop()
    assert job["state"] == "failed", job
    assert job["failure_type"] == "invalid_input"
    (attempt,) = job["attempts"]  # never retried
    assert attempt["detail"].startswith("secret_bytes_over_limit:"), attempt
    assert f"total {CAP + 1} bytes" in attempt["detail"]
    assert not (tmp_path / "ran.txt").exists()  # nothing reached the node
    assert grown.hex() not in str(job)
