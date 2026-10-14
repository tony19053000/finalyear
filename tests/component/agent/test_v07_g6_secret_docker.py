"""V0.7 G6 deliverable 4 (D144), Docker: a job's secrets are files at
``/run/secrets/NAME`` - read-only, ``0400``, owned by the container user, on a
RAM-backed host folder - and the create-time sandbox check accepts exactly that
one extra bind. The workload prints SHA-256s (never values); after the run the
agent's secret folder is empty.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
    pytest.mark.docker,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers"),
]

KEY = b"docker-file-secret-123"
WORKLOAD = r"""
import hashlib, json, os, stat
p = "/run/secrets/API_KEY"
info = os.stat(p)
try:
    open(p, "ab").close()
    writable = True
except OSError:
    writable = False
print(json.dumps({
    "sha": hashlib.sha256(open(p, "rb").read()).hexdigest(),
    "mode": oct(stat.S_IMODE(info.st_mode)),
    "owner_is_me": info.st_uid == os.getuid(),
    "writable": writable,
    "env_dir": os.environ.get("MESHFORGE_SECRETS_DIR"),
    "in_env": "API_KEY" in os.environ,
}))
"""


def _docker_ready() -> bool:
    try:
        from meshforge.agent.execution.docker_support import open_client
        from meshforge.agent.settings import PYTHON_CPU_IMAGE

        client = open_client(10)
    except Exception:  # noqa: BLE001
        return False
    try:
        client.images.get(PYTHON_CPU_IMAGE)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


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
    engine = create_engine(pg_settings)
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
        yield client


@pytest.mark.security
async def test_docker_gets_a_read_only_secret_file(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    if not await asyncio.to_thread(_docker_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": False}},
        docker=True,
    )
    try:
        assert agent.setup.runtimes.keys() == {"docker"}
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text(WORKLOAD)
        job_id = await submit(
            alice,
            project,
            agent.node_id,
            {
                "name": "g6-docker",
                "entrypoint": ["python", "main.py"],
                "secrets": ["API_KEY"],
            },
        )
        job = await wait_job(alice, job_id, timeout_s=120)
        assert job["state"] == "succeeded", job
        logs = await alice.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
        seen = json.loads(logs.content.decode().strip().splitlines()[-1])
        assert seen == {
            "sha": hashlib.sha256(KEY).hexdigest(),
            "mode": "0o400",
            "owner_is_me": True,
            "writable": False,
            "env_dir": "/run/secrets",
            "in_env": False,
        }
        assert KEY not in logs.content
        assert list(agent.executor.vault.base.iterdir()) == []
    finally:
        await agent.stop()
