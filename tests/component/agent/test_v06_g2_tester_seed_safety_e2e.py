"""V0.6 G2 tester: seeding must never make a job wrong (D109 safety).

The submitter's own agent cache already holds a *corrupt* file under the
digest of a project file (bit rot, an earlier crash, an attacker): the job
that is seeded and placed Local must still read the correct bytes - the agent
verifies what it takes from its cache - and the job must succeed.
"""

from __future__ import annotations

import asyncio
import hashlib
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

from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

PAYLOAD = os.urandom(300_000)
READER = (
    "import hashlib\n"
    "print('SHA', hashlib.sha256(open('weights.bin','rb').read()).hexdigest(), flush=True)\n"
)


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.05},
        planner={"default_policy": "cost_model"},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def local(grpc_target: str, tmp_path: Path) -> AsyncIterator[ExecAgent]:
    agent = await ExecAgent.start(grpc_target, tmp_path / "local-data")
    yield agent
    await agent.stop()


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


async def test_a_corrupt_blob_already_in_the_local_cache_never_reaches_the_job(
    runtime: ControllerRuntime, local: ExecAgent, tmp_path: Path
) -> None:
    data_dir = tmp_path / "local-data"
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(READER)
    (project / "weights.bin").write_bytes(PAYLOAD)
    digest = Digest(hashlib.sha256(PAYLOAD).hexdigest())
    store = CasStore(data_dir / "cas", max_blob_bytes=1 << 30)
    victim = store.path_for(digest)
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"CORRUPT" * 1000)
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": f"http://127.0.0.1:{runtime.http_port}",
            "MESHFORGE_CLI__TOKEN": token_of(runtime),
            "MESHFORGE_DATA_DIR": str(data_dir),
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
        }
    )
    code, out, err = await _compute(env, "run", str(project))
    assert code == 0, err
    job_id = out.strip()
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as http:
        job = await wait_job(http, job_id, timeout_s=90)
        assert job["state"] == "succeeded", job
        logs = await http.get(f"/v1/jobs/{job_id}/logs")
    text = logs.text
    assert f"SHA {digest.hex}" in text, text[-800:]
