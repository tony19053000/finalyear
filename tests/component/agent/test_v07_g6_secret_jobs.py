"""V0.7 G6 deliverable 3 (D144): manifest ``secrets:`` resolve to the JOB
OWNER's secrets only; a missing one fails the job typed before any node starts.

Real mTLS controller, real PostgreSQL, a real agent in this loop.

* submit naming a secret the user lacks (or another user's) -> 400
  ``secret_missing:<NAME>``, no job row;
* a secret deleted while the job waits in the queue -> at dispatch the attempt
  ends ``FAILED(invalid_input)`` ``secret_missing:<NAME>`` with nothing sent
  to the node (the workload never ran), one attempt, never retried, never hangs;
* the value appears in no job/decision/explain response.
"""

from __future__ import annotations

import base64
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
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

PW = {"alice": "alice-password-1", "bob": "bob-password-1"}
VALUE = b"queue-time-secret-value"
WORKLOAD = "import sys\nopen(sys.argv[1], 'w').write('ran')\n"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def users(
    engine: AsyncEngine, runtime: ControllerRuntime
) -> AsyncIterator[dict[str, httpx.AsyncClient]]:
    base = f"https://127.0.0.1:{runtime.http_port}"
    made: dict[str, httpx.AsyncClient] = {}
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        for name, pw in PW.items():
            await create_user(engine, name, pw)
            token = await login(anon, name, pw)
            made[name] = httpx.AsyncClient(
                base_url=base, headers=bearer(token), timeout=30
            )
    try:
        yield made
    finally:
        for client in made.values():
            await client.aclose()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(WORKLOAD)
    return project


def _manifest(tmp_path: Path, secrets: list[Any]) -> dict[str, Any]:
    return {
        "name": "g6-secrets",
        "entrypoint": ["python", "main.py", str(tmp_path / "ran.txt")],
        "secrets": secrets,
    }


async def _agent(grpc_target: str, data_dir: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )


@pytest.mark.security
async def test_submit_refuses_a_secret_the_owner_lacks(
    users: dict[str, httpx.AsyncClient], grpc_target: str, tmp_path: Path
) -> None:
    alice, bob = users["alice"], users["bob"]
    agent = await _agent(grpc_target, tmp_path / "a")
    try:
        assert (await bob.put("/v1/secrets/BOBS", content=b"bob-secret")).is_success
        for names in (["NOPE"], ["BOBS"], [{"name": "nope", "as_env": True}]):
            with pytest.raises(AssertionError) as exc:
                await submit(
                    alice, _project(tmp_path), None, _manifest(tmp_path, names)
                )
            text = str(exc.value)
            assert "secret_missing:" in text
            assert "bob-secret" not in text
        listed = (await alice.get("/v1/jobs")).json()
        assert listed.get("jobs", []) == []
        assert not (tmp_path / "ran.txt").exists()
    finally:
        await agent.stop()


@pytest.mark.security
async def test_a_secret_deleted_while_queued_fails_the_job_before_the_node(
    users: dict[str, httpx.AsyncClient], grpc_target: str, tmp_path: Path
) -> None:
    alice = users["alice"]
    assert (await alice.put("/v1/secrets/API_KEY", content=VALUE)).is_success
    first = await _agent(grpc_target, tmp_path / "a")
    await first.stop()  # enrolled, offline: the job waits in the queue
    job_id = await submit(
        alice, _project(tmp_path), None, _manifest(tmp_path, ["api_key"])
    )
    queued = (await alice.get(f"/v1/jobs/{job_id}")).json()
    assert queued["state"] in ("queued", "planning", "submitted"), queued
    assert (await alice.delete("/v1/secrets/API_KEY")).is_success
    again = await _agent(grpc_target, tmp_path / "a")
    try:
        job = await wait_job(alice, job_id, timeout_s=30)
    finally:
        await again.stop()
    assert job["state"] == "failed", job
    assert job["failure_type"] == "invalid_input"
    assert "secret_missing:API_KEY" in (job["failure_detail"] or "")
    (attempt,) = job["attempts"]  # never retried
    assert attempt["state"] == "failed"
    assert attempt["detail"].startswith("secret_missing:API_KEY")
    assert not (tmp_path / "ran.txt").exists()  # nothing reached the node
    bodies = [
        (await alice.get(f"/v1/jobs/{job_id}")).content,
        (await alice.get(f"/v1/jobs/{job_id}/decisions")).content,
    ]
    for body in bodies:
        assert VALUE not in body and base64.b64encode(VALUE) not in body
