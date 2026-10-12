"""V0.7 G6 deliverable 4 (D144): secrets travel in ``StartAttempt`` over mTLS and
are materialised for the run only, then wiped on every exit path.

Real mTLS controller, real PostgreSQL, a real agent (subprocess runtime) in this
loop. The workload proves what it saw by writing the SHA-256 of what it read
(never the value) next to the test:

* the file ``$MESHFORGE_SECRETS_DIR/API_KEY`` holds the value, mode ``0600``
  in a ``0500`` folder; ``DB_PASS`` (``as_env``) is also an env variable,
  ``API_KEY`` is not;
* after the attempt ends - success, failure, cancel, agent stop - the agent's
  secret folder is empty;
* a leftover folder of a "crashed" agent is wiped at the next start.
The Docker side is ``test_v07_g6_secret_docker.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
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

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes"),
]

KEY = b"file-secret-0123456789"
PASS = b"env-secret-abcdef"
WORKLOAD = r"""
import hashlib, json, os, stat, sys, time
out, mode = sys.argv[1], sys.argv[2]
folder = os.environ["MESHFORGE_SECRETS_DIR"]
path = os.path.join(folder, "API_KEY")
data = open(path, "rb").read()
json.dump({
    "folder": folder,
    "file_sha": hashlib.sha256(data).hexdigest(),
    "file_mode": oct(stat.S_IMODE(os.stat(path).st_mode)),
    "dir_mode": oct(stat.S_IMODE(os.stat(folder).st_mode)),
    "env_sha": hashlib.sha256(os.environ.get("DB_PASS", "").encode()).hexdigest(),
    "api_key_in_env": "API_KEY" in os.environ,
}, open(out, "w"))
if mode == "fail":
    sys.exit(3)
if mode == "sleep":
    time.sleep(60)
"""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def alice(
    pg_settings: DatabaseSettings, runtime: ControllerRuntime
) -> AsyncIterator[httpx.AsyncClient]:
    engine: AsyncEngine = create_engine(pg_settings)
    try:
        await create_user(engine, "alice", "alice-password-1")
    finally:
        await engine.dispose()
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        token = await login(anon, "alice", "alice-password-1")
    async with httpx.AsyncClient(
        base_url=base, headers=bearer(token), timeout=30
    ) as client:
        assert (await client.put("/v1/secrets/API_KEY", content=KEY)).is_success
        assert (await client.put("/v1/secrets/DB_PASS", content=PASS)).is_success
        yield client


async def _agent(grpc_target: str, data_dir: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(WORKLOAD)
    return project


def _manifest(tmp_path: Path, mode: str) -> dict[str, Any]:
    return {
        "name": "g6-delivery",
        "entrypoint": ["python", "main.py", str(tmp_path / "seen.json"), mode],
        "secrets": ["API_KEY", {"name": "DB_PASS", "as_env": True}],
        "retry": {"max_attempts": 1},
    }


def _seen(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "seen.json").read_text()))


def _check_seen(seen: dict[str, Any]) -> None:
    assert seen["file_sha"] == _sha(KEY)
    assert seen["env_sha"] == _sha(PASS)
    assert seen["file_mode"] == "0o600" and seen["dir_mode"] == "0o500"
    assert seen["api_key_in_env"] is False


async def _wait_seen(tmp_path: Path) -> None:
    for _ in range(400):
        if (tmp_path / "seen.json").exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the workload never wrote what it saw")


@pytest.mark.parametrize(("mode", "state"), [("ok", "succeeded"), ("fail", "failed")])
async def test_delivered_then_wiped_on_success_and_failure(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    mode: str,
    state: str,
) -> None:
    agent = await _agent(grpc_target, tmp_path / "agent")
    try:
        job_id = await submit(
            alice, _project(tmp_path), None, _manifest(tmp_path, mode)
        )
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == state, job
        _check_seen(_seen(tmp_path))
        assert Path(_seen(tmp_path)["folder"]).parent == agent.executor.vault.base
        assert list(agent.executor.vault.base.iterdir()) == []
    finally:
        await agent.stop()


@pytest.mark.parametrize("how", ["cancel", "agent_stop"])
async def test_wiped_on_cancel_and_agent_stop(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path, how: str
) -> None:
    agent = await _agent(grpc_target, tmp_path / "agent")
    base = agent.executor.vault.base
    try:
        job_id = await submit(
            alice, _project(tmp_path), None, _manifest(tmp_path, "sleep")
        )
        await _wait_seen(tmp_path)
        _check_seen(_seen(tmp_path))
        assert len(list(base.iterdir())) == 1  # present while it runs
        if how == "cancel":
            assert (await alice.post(f"/v1/jobs/{job_id}/cancel")).is_success
            job = await wait_job(alice, job_id, timeout_s=60)
            assert job["state"] == "cancelled", job
        else:
            await agent.stop()
        for _ in range(200):
            if not list(base.iterdir()):
                break
            await asyncio.sleep(0.05)
        assert list(base.iterdir()) == []
    finally:
        await agent.stop()


async def test_a_crashed_agents_files_are_swept_at_start(
    grpc_target: str, tmp_path: Path, runtime: ControllerRuntime
) -> None:
    first = await _agent(grpc_target, tmp_path / "agent")
    vault = first.executor.vault
    await first.stop()
    left = vault.materialise("att_crashed", {"K": b"left-behind"}, read_only=False)
    assert left.exists()
    again = await _agent(grpc_target, tmp_path / "agent")
    try:
        assert not left.exists()
        assert list(vault.base.iterdir()) == []
    finally:
        await again.stop()
