"""V0.5 G1 criteria 1, 3, 4 on the agent side: real PostgreSQL + gRPC + REST + agent.

Tree jobs are submitted the way ``compute run`` does (``cli.transfer.upload_tree``
then ``POST /v1/jobs`` with ``tree_id``) and run through the subprocess runtime.
What the agent fetched is read from ``Executor.last_stage``; a killed fetch is a
real process SIGKILLed mid-file.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent, wait_job
from mf_jobs_harness import token_of
from pydantic import SecretStr

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobSource
from meshforge.agent.execution.fetch import stage_tree
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.hashing import sha256_bytes
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE

MAIN = b"""\
import sys
with open(sys.argv[1], "a") as fh:
    fh.write(open("lib.txt").read())
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
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
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


async def _upload(runtime: ControllerRuntime, built: BuiltTree) -> None:
    client = ControllerClient(
        f"https://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()


async def _run(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    project: Path,
    node_id: str,
    counter: Path,
) -> dict[str, Any]:
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    posted = await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "tree",
                "entrypoint": ["python", "main.py", str(counter)],
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node_id},
        },
    )
    assert posted.status_code == 201, posted.text
    return await wait_job(http, str(posted.json()["job_id"]))


def _project(root: Path, lib: bytes = b"one\n") -> Path:
    root.mkdir()
    (root / "main.py").write_bytes(MAIN)
    (root / "lib.txt").write_bytes(lib)
    (root / "data.bin").write_bytes(os.urandom(3 * CS + 1))  # chunked
    return root


async def test_tree_job_resubmit_fetches_nothing_and_one_change_one_blob(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "p")
    counter = tmp_path / "out.txt"
    job = await _run(runtime, http, project, agent.node_id, counter)
    assert job["state"] == "succeeded", job
    assert job["tree_id"] and job["bundle_digest"] is None
    first = agent.executor.last_stage
    assert first is not None and first.blobs_fetched == 3 and first.blobs_hit == 0

    job = await _run(runtime, http, project, agent.node_id, counter)
    assert job["state"] == "succeeded", job
    again = agent.executor.last_stage
    assert again is not None and again is not first
    assert again.bytes_fetched == 0 and again.blobs_fetched == 0  # criterion 1

    (project / "lib.txt").write_bytes(b"two\n")
    job = await _run(runtime, http, project, agent.node_id, counter)
    assert job["state"] == "succeeded", job
    changed = agent.executor.last_stage
    assert changed is not None and changed.blobs_fetched == 1
    manifest = build_tree(project, chunk_size=CS).manifest
    assert changed.bytes_fetched == len(b"two\n") + len(manifest)
    assert counter.read_text() == "one\none\ntwo\n"


async def test_a_corrupt_blob_at_the_controller_fails_integrity_and_never_runs(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "p", lib=b"fresh bytes\n")
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    assert runtime.blobs is not None
    stored = runtime.blobs.path_for(sha256_bytes(b"fresh bytes\n"))
    stored.chmod(0o600)
    stored.write_bytes(b"FRESH BYTES\n")  # same size: real on-disk corruption
    counter = tmp_path / "out.txt"
    job = await _run(runtime, http, project, agent.node_id, counter)
    assert job["state"] == "failed", job
    # integrity_error is retryable (V0.6 D110), so the job may have retried;
    # once the scrubber has removed the corrupt blob, a retry fails not_found.
    attempts = job["attempts"]
    assert attempts[0]["failure_type"] == "integrity_error", attempts
    for attempt in attempts:
        assert attempt["failure_type"] in {"integrity_error", "not_found"}, attempt
    assert job["failure_type"] == attempts[-1]["failure_type"], job
    assert not counter.exists()  # the workload never ran on corrupt input
    assert not agent.executor.cas.store.has(sha256_bytes(b"fresh bytes\n"))
    assert not agent.executor.cas.store.has(sha256_bytes(b"FRESH BYTES\n"))


FETCHER = """
import asyncio, sys, time
from pathlib import Path
import httpx
from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobSource
from meshforge.agent.execution.fetch import stage_tree
from meshforge.common.cas import CasStore

class Slow(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        if "range" in request.headers:
            await asyncio.sleep(0.25)
        return await super().handle_async_request(request)

url, token, tree_id, cas_dir, dest = sys.argv[1:6]
cas = AgentCas(CasStore(Path(cas_dir), max_blob_bytes=1 << 30))
asyncio.run(stage_tree(cas, tree_id=tree_id, sources=[BlobSource(url, token)],
                       dest=Path(dest), transport=Slow(), free_disk_floor_bytes=0))
print("finished")
"""


async def test_killed_agent_fetch_resumes_without_refetching_chunks(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    data = os.urandom(10 * CS)
    (project / "big.bin").write_bytes(data)
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint("attempt_k", [built.tree.tree_id], ttl_s=120)
    url = f"https://127.0.0.1:{runtime.http_port}"
    cas_dir = tmp_path / "agent-cas"
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        textwrap.dedent(FETCHER),
        url,
        token.get_secret_value(),
        str(built.tree.tree_id),
        str(cas_dir),
        str(tmp_path / "w1"),
        stdout=asyncio.subprocess.DEVNULL,
    )
    digest = sha256_bytes(data)
    sidecar = cas_dir / "tmp" / f"resume-{digest.hex}.part.chunks"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if sidecar.exists() and sidecar.read_text().count('"done"') == 1:
                done_hex = sidecar.read_text().split('"done":"')[1].split('"')[0]
                if bin(int(done_hex or "0", 16)).count("1") >= 3:
                    break
            assert child.returncode is None, "the fetch finished before the kill"
            await asyncio.sleep(0.05)
        child.kill()  # mid-file, for real
        await asyncio.wait_for(child.wait(), 10)
    finally:
        if child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.wait(), 10)
    cas = AgentCas(CasStore(cas_dir, max_blob_bytes=1 << 30))
    assert not cas.store.has(digest)
    report = await stage_tree(
        cas,
        tree_id=str(built.tree.tree_id),
        sources=[BlobSource(url, token.get_secret_value())],
        dest=tmp_path / "w2",
        free_disk_floor_bytes=0,
    )
    assert report.chunks_resumed >= 3
    # Completed chunks never travel again; the manifest was cached too.
    assert report.bytes_fetched == (10 - report.chunks_resumed) * CS
    assert (tmp_path / "w2" / "big.bin").read_bytes() == data
