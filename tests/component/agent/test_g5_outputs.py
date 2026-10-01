"""V0.4 G5 D74 end to end: declared outputs come back byte-identical.

Real PostgreSQL + gRPC + REST + a real agent (subprocess runtime). The workload
writes files; the agent packs the declared outputs, uploads them with the
attempt's upload grant, and the controller records ``output_digest`` only after
checking that grant stored it. The test then downloads the blob with the fabric
token, unpacks it (hostile-input-safe) and compares bytes.
"""

from __future__ import annotations

import hashlib
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
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.common.bundle import unpack
from meshforge.common.states import AttemptPhase
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

WRITER = """\
import os, sys
os.makedirs("results/sub", exist_ok=True)
with open("results/a.bin", "wb") as fh:
    fh.write(bytes(range(256)) * 4000)
with open("results/sub/b.txt", "w") as fh:
    fh.write("hello outputs\\n")
with open("scratch.tmp", "w") as fh:
    fh.write("not declared")
if len(sys.argv) > 1:
    with open("big.bin", "wb") as fh:
        fh.write(os.urandom(int(sys.argv[1])))
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    # Not a liveness test (see TOLERANT_LIVENESS): under load FAST_LIVENESS's
    # 0.6 s lease expired mid-run and the job ended node_lost.
    return make_controller_settings(
        pg_settings, jobs={"dispatch_interval_s": 0.1}, **tolerant_liveness()
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=60,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


@pytest_asyncio.fixture
async def store(pg_settings: Any) -> AsyncIterator[ExecutionStore]:
    engine = create_engine(pg_settings)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WRITER)
    return project


async def _download(http: httpx.AsyncClient, digest: str, dest: Path) -> Path:
    resp = await http.get(f"/v1/blobs/{digest.removeprefix('sha256:')}")
    assert resp.status_code == 200, resp.text
    assert hashlib.sha256(resp.content).hexdigest() == digest.removeprefix("sha256:")
    archive = dest.with_suffix(".bundle")
    archive.write_bytes(resp.content)
    unpack(archive, dest)
    return dest


async def test_declared_outputs_round_trip_byte_identical(
    http: httpx.AsyncClient, agent: ExecAgent, store: ExecutionStore, tmp_path: Path
) -> None:
    manifest = {
        "name": "outputs",
        "entrypoint": ["python", "main.py"],
        "outputs": ["results/", "never/written.csv"],
    }
    job_id = await submit(http, _project(tmp_path), agent.node_id, manifest)
    job = await wait_job(http, job_id, timeout_s=30)
    assert job["state"] == "succeeded", job
    (attempt,) = job["attempts"]
    assert attempt["missing_outputs"] == ["never/written.csv"]
    digest = attempt["output_digest"]
    assert digest and digest.startswith("sha256:")
    out = await _download(http, digest, tmp_path / "got")
    assert (out / "results" / "a.bin").read_bytes() == bytes(range(256)) * 4000
    assert (out / "results" / "sub" / "b.txt").read_bytes() == b"hello outputs\n"
    assert not (out / "scratch.tmp").exists() and not (out / "main.py").exists()
    phases = {p.phase: p for p in await store.phases(attempt["attempt_id"])}
    assert not phases[AttemptPhase.COLLECT.value].skipped


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need Developer Mode")
async def test_a_symlinked_output_fails_output_error(
    http: httpx.AsyncClient, agent: ExecAgent, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("import os\nos.symlink('/etc/hostname', 'leak')\n")
    manifest = {
        "name": "leak",
        "entrypoint": ["python", "main.py"],
        "outputs": ["leak"],
    }
    job_id = await submit(http, project, agent.node_id, manifest)
    job = await wait_job(http, job_id, timeout_s=30)
    assert job["state"] == "failed" and job["failure_type"] == "output_error", job
    (attempt,) = job["attempts"]
    assert attempt["exit_code"] == 0 and attempt["output_digest"] is None


async def test_a_100_mb_output_uploads(
    http: httpx.AsyncClient, agent: ExecAgent, tmp_path: Path
) -> None:
    """Not a benchmark claim: the Laptop 1 loopback time of a 100 MB output."""
    size = 100 * 1024 * 1024
    manifest = {
        "name": "big",
        "entrypoint": ["python", "main.py", str(size)],
        "outputs": ["big.bin"],
    }
    started = time.monotonic()
    job_id = await submit(http, _project(tmp_path), agent.node_id, manifest)
    job = await wait_job(http, job_id, timeout_s=120)
    took = time.monotonic() - started
    assert job["state"] == "succeeded", job
    (attempt,) = job["attempts"]
    out = await _download(http, attempt["output_digest"], tmp_path / "big")
    assert (out / "big.bin").stat().st_size == size
    print(  # noqa: T201 - a measured number for the handoff (run with -s)
        f"Laptop 1 (not a benchmark claim): 100 MB output job submit->succeeded "
        f"{took:.2f} s"
    )
