"""V0.7 G6 fix round 3, T-21 (D164): an agent whose secrets base is planted.

Another local user created the agent's (predictable) base name as a symlink
to a directory. Real mTLS controller, PostgreSQL, a real agent in this loop:

* the agent starts (its start sweep refuses the base and touches nothing);
* a job WITH secrets fails typed - ``config_error``,
  ``secrets_base_refused:symlink`` - and its workload never runs;
* a job without secrets runs as usual;
* the owner removes the planted name: the next secrets job works, no restart.
"""

from __future__ import annotations

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
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.agent.execution.secret_files import (
    SecretVault,
    agent_tag,
    default_root,
)
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

WORKLOAD = r"""
import os, sys
folder = os.environ.get("MESHFORGE_SECRETS_DIR")
data = open(os.path.join(folder, "API_KEY"), "rb").read() if folder else b""
open(sys.argv[1], "w").write(f"ran {len(data)}")
"""


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
        assert (
            await client.put("/v1/secrets/API_KEY", content=b"fix3-secret-0123456789")
        ).is_success
        yield client


def _manifest(out: Path, *, secrets: bool) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": "g6-fix3-base",
        "entrypoint": ["python", "main.py", str(out)],
        "retry": {"max_attempts": 1},
    }
    if secrets:
        manifest["secrets"] = ["API_KEY"]
    return manifest


async def test_a_planted_base_fails_secret_jobs_typed_and_nothing_else(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    data_dir = tmp_path / "agent"
    victim = tmp_path / "victim"
    (victim / "att_precious").mkdir(parents=True)
    (victim / "att_precious" / "f").write_bytes(b"keep")
    planted = SecretVault(default_root(data_dir), tag=agent_tag(data_dir)).base
    os.symlink(victim, planted)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORKLOAD)
    try:
        agent = await ExecAgent.start(
            grpc_target,
            data_dir,
            agent_overrides={
                "policy": {"accept_jobs": True, "allow_unsandboxed": True}
            },
        )
        try:
            assert agent.executor.vault.base == planted  # the start sweep saw it
            assert (victim / "att_precious" / "f").read_bytes() == b"keep"

            out = tmp_path / "with-secrets.txt"
            job_id = await submit(alice, project, None, _manifest(out, secrets=True))
            job = await wait_job(alice, job_id, timeout_s=60)
            assert job["state"] == "failed", job
            assert job["failure_type"] == "config_error", job
            assert "secrets_base_refused:symlink" in (job["failure_detail"] or ""), job
            assert str(victim) not in str(job)
            assert not out.exists(), "the workload ran without a safe secrets folder"
            assert os.listdir(victim) == ["att_precious"]
            assert (victim / "att_precious" / "f").read_bytes() == b"keep"

            plain = tmp_path / "plain.txt"
            job_id = await submit(alice, project, None, _manifest(plain, secrets=False))
            job = await wait_job(alice, job_id, timeout_s=60)
            assert job["state"] == "succeeded", job
            assert plain.read_text() == "ran 0"

            planted.unlink()  # the owner repairs it; no restart
            job_id = await submit(alice, project, None, _manifest(out, secrets=True))
            job = await wait_job(alice, job_id, timeout_s=60)
            assert job["state"] == "succeeded", job
            assert out.read_text() == "ran 22"
            assert os.listdir(planted) == []
        finally:
            await agent.stop()
    finally:
        if planted.is_symlink():
            planted.unlink()
        elif planted.is_dir():
            planted.rmdir()
    assert (victim / "att_precious" / "f").read_bytes() == b"keep"
