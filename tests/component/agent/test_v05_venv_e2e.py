"""V0.5 G2 end to end (D86, criterion 5): a requirements job through the **venv
runtime** on a real controller + PostgreSQL + agent, twice - the first attempt
builds the environment, the second reuses it (no install) and reports
``runtime = venv``. Installs are offline (wheelhouse + ``--no-index``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import structlog

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("uv", reason="needs the agent extra (the uv wheel)")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_envs_support import make_wheel
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

ENV_OVERHEAD_BOUND_S = 3.0  # V0.5 criterion 5
MAIN = """\
import sys, mfvenvdemo
print("VERSION", mfvenvdemo.VERSION, "PREFIX", sys.prefix, flush=True)
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        # not a liveness test: in-loop agent, a loaded runner expires a 0.6 s lease (D399)
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def store(pg_settings: Any) -> AsyncIterator[ExecutionStore]:
    engine = create_engine(pg_settings)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


def _project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(MAIN)
    (project / "req.txt").write_text("# the demo package\nmfvenvdemo==1.0\n")
    return project


MANIFEST = {
    "name": "venv-e2e",
    "entrypoint": ["python", "main.py"],
    "env": {"requirements": "req.txt"},
}


async def test_requirements_job_twice_through_the_venv_runtime(
    http: httpx.AsyncClient,
    store: ExecutionStore,
    grpc_target: str,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    wheels = tmp_path / "wheels"
    make_wheel(wheels, "mfvenvdemo", "1.0")
    agent = await ExecAgent.start(
        grpc_target,
        data_dir,
        venv=True,
        agent_overrides={
            "envs": {
                "find_links": str(wheels),
                "no_index": True,
                "free_disk_floor_mb": 0,
            }
        },
    )
    try:
        assert "venv" in agent.setup.runtimes
        records = []
        with structlog.testing.capture_logs() as logs:
            for name in ("first", "second"):
                job_id = await submit(
                    http, _project(tmp_path, name), agent.node_id, MANIFEST
                )
                job = await wait_job(http, job_id, timeout_s=90)
                assert job["state"] == "succeeded", job
                (attempt,) = job["attempts"]
                record = await store.get_attempt(attempt["attempt_id"])
                assert record is not None and record.runtime == "venv"
                env_phase = {p.phase: p for p in await store.phases(record.id)}["env"]
                records.append((attempt["attempt_id"], env_phase))
    finally:
        await agent.stop()
    ready = [e for e in logs if e.get("event") == "agent.execution.env_ready"]
    assert [e["env_cache"] for e in ready] == ["built", "hit"]
    assert ready[0]["env_hash"] == ready[1]["env_hash"]
    assert [e["attempt_id"] for e in ready] == [a for a, _ in records]
    envs = sorted(p.name for p in (data_dir / "envs" / "venv").iterdir())
    assert envs == [ready[0]["env_hash"]]  # built once, reused
    cold, warm = (phase for _, phase in records)
    assert not warm.skipped and warm.duration_ms is not None
    assert warm.duration_ms <= ENV_OVERHEAD_BOUND_S * 1000
    print(  # noqa: T201 - measured, for the handoff (run with -s)
        f"Laptop 1 (not a benchmark claim): venv e2e env phase cold "
        f"{cold.duration_ms} ms, warm {warm.duration_ms} ms"
    )
