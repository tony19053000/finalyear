"""V0.5 D95: a controller restart in the middle of an attempt's fetch.

Real PostgreSQL + gRPC + REST + a real agent (subprocess runtime). The
controller is stopped while the agent is part-way through staging, the agent's
next request meets a dead port (connection refused), then a fresh
:class:`ControllerRuntime` comes up on the same ports and database. Its token
table is empty - the old fetch token is refused - and after the agent's
Reconcile it sends ``RefreshFetchToken``; staging resumes with it.

"Mid-fetch" is made deterministic by holding the agent's next request at a
gate (a chunk ``Range`` request for a tree job, the bundle ``GET`` for a bundle
job) until the controller is down.

Checked: the job SUCCEEDS; a tree job's chunks verified before the restart are
not fetched again; the pre-restart token is refused by the new controller; with
no refresh coming, staging gives up at ``staging_retry_s`` as
``transfer_error``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_fabric import client_ssl_context
from mf_jobs_harness import HARNESS_NODE_ID, token_of
from pydantic import SecretStr

from meshforge.agent.cas.fetcher import BlobFetcher
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE
CHUNKS = 6
HOLD_AT_CHUNK = 3
"""Chunk requests 1..2 complete before the restart; the 3rd is held."""

MAIN = b"""\
import hashlib, sys
data = open("data.bin", "rb").read()
open(sys.argv[1], "w").write(hashlib.sha256(data).hexdigest())
"""


def _settings(pg: DatabaseSettings, **binds: str) -> ControllerSettings:
    return make_controller_settings(
        pg,
        jobs={"dispatch_interval_s": 0.1},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
        **tolerant_liveness(),  # not a liveness test
        **binds,
    )


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return _settings(pg_settings)


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(
        grpc_target,
        data_dir,
        execution={"staging_retry_s": 30},
        # One request at a time: "the 3rd chunk request is held" stays exact
        # with the V0.5 G4 multi-request fetcher.
        agent_overrides={"fetch": {"per_source": 1}},
    )
    yield running
    await running.stop()


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    )


class Gate:
    """Holds the n-th call of a wrapped coroutine function until released."""

    def __init__(self, at: int) -> None:
        self.at = at
        self.calls = 0
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.tokens: list[str] = []

    async def hold(self) -> None:
        self.calls += 1
        if self.calls == self.at:
            self.reached.set()
            await self.release.wait()


@contextlib.asynccontextmanager
async def _restart_mid_fetch(
    runtime: ControllerRuntime,
    pg: DatabaseSettings,
    gate: Gate,
    *,
    patch_fresh: Callable[[ControllerRuntime], None] | None = None,
) -> AsyncIterator[ControllerRuntime]:
    await asyncio.wait_for(gate.reached.wait(), 30)
    grpc_port, http_port = runtime.grpc_port, runtime.http_port
    await runtime.stop()
    gate.release.set()  # the held request now meets a dead port
    await asyncio.sleep(0.5)
    fresh_settings = _settings(
        pg, grpc_bind=f"127.0.0.1:{grpc_port}", http_bind=f"127.0.0.1:{http_port}"
    )
    async with ControllerRuntime.start(fresh_settings) as fresh:
        if patch_fresh is not None:
            patch_fresh(fresh)
        yield fresh


async def _stale_token_refused(
    fresh: ControllerRuntime, digest_hex: str, token: str
) -> None:
    # with a valid node certificate: the token itself must be what is refused
    async with httpx.AsyncClient(
        timeout=10, verify=client_ssl_context(node_id=HARNESS_NODE_ID)
    ) as raw:
        got = await raw.get(
            f"https://127.0.0.1:{fresh.http_port}/v1/blobs/{digest_hex}",
            headers={"authorization": f"Bearer {token}"},
        )
    assert got.status_code == 401, got.status_code


async def test_tree_job_survives_a_controller_restart_mid_fetch_and_resumes(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    agent: ExecAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_bytes(MAIN)
    data = os.urandom(CHUNKS * CS)
    (project / "data.bin").write_bytes(data)
    built = build_tree(project, chunk_size=CS)
    client = ControllerClient(
        f"https://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()

    gate = Gate(HOLD_AT_CHUNK)
    real_range = BlobFetcher._range

    async def gated_range(
        self: BlobFetcher, source: Any, digest: Any, start: int, length: int
    ) -> bytes:
        gate.tokens.append(source.src.token)
        await gate.hold()
        return await real_range(self, source, digest, start, length)

    monkeypatch.setattr(BlobFetcher, "_range", gated_range)
    counter = tmp_path / "out.txt"
    async with _http(runtime) as http:
        posted = await http.post(
            "/v1/jobs",
            json={
                "manifest": {
                    "name": "tree-restart",
                    "entrypoint": ["python", "main.py", str(counter)],
                },
                "tree_id": str(built.tree.tree_id),
                "placement": {"node_id": agent.node_id},
            },
        )
        assert posted.status_code == 201, posted.text
        job_id = str(posted.json()["job_id"])

    data_entry = next(e for e in built.tree.files() if e.chunks)
    async with (
        _restart_mid_fetch(runtime, pg_settings, gate) as fresh,
        _http(fresh) as http2,
    ):
        job = await wait_job(http2, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        old_token = gate.tokens[0]
        await _stale_token_refused(fresh, data_entry.sha256, old_token)
        assert fresh.dispatcher is not None
        assert fresh.dispatcher.stats.fetch_tokens_refreshed == 1

    assert counter.read_text() == hashlib.sha256(data).hexdigest()
    stage = agent.executor.last_stage
    assert stage is not None
    # the 2 chunks verified before the restart were kept, not fetched again
    assert stage.chunks_resumed == HOLD_AT_CHUNK - 1
    assert stage.bytes_fetched == (CHUNKS - (HOLD_AT_CHUNK - 1)) * CS
    assert gate.tokens[-1] != old_token  # the retry used the refreshed token


async def test_bundle_job_survives_a_controller_restart_mid_fetch(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("print('bundle ok')\n")
    gate = Gate(1)
    real_fetch = agent.executor._fetch

    async def gated_fetch(
        base: str, digest: Any, token: str, dest: Path, **kw: Any
    ) -> int:
        gate.tokens.append(token)
        await gate.hold()
        return await real_fetch(base, digest, token, dest, **kw)

    agent.executor._fetch = gated_fetch
    async with _http(runtime) as http:
        job_id = await submit(
            http,
            project,
            agent.node_id,
            {"name": "bundle-restart", "entrypoint": ["python", "main.py"]},
        )
        digest_hex = (await http.get(f"/v1/jobs/{job_id}")).json()["bundle_digest"]
    async with (
        _restart_mid_fetch(runtime, pg_settings, gate) as fresh,
        _http(fresh) as http2,
    ):
        job = await wait_job(http2, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        assert len(job["attempts"]) == 1
        await _stale_token_refused(
            fresh, digest_hex.removeprefix("sha256:"), gate.tokens[0]
        )
    assert len(set(gate.tokens)) >= 2  # old token, then the refreshed one


async def test_no_fresh_token_within_the_retry_deadline_is_transfer_error(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    short = await ExecAgent.start(
        grpc_target, tmp_path / "agent-short", execution={"staging_retry_s": 2}
    )
    try:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text("print('never')\n")
        gate = Gate(1)
        real_fetch = short.executor._fetch

        async def gated_fetch(
            base: str, digest: Any, token: str, dest: Path, **kw: Any
        ) -> int:
            await gate.hold()
            return await real_fetch(base, digest, token, dest, **kw)

        short.executor._fetch = gated_fetch

        def no_refresh(fresh: ControllerRuntime) -> None:
            assert fresh.dispatcher is not None
            noop: Callable[..., None] = lambda *_a, **_k: None  # noqa: E731
            fresh.dispatcher._refresh_fetch_token = noop  # type: ignore[method-assign]

        async with _http(runtime) as http:
            job_id = await submit(
                http,
                project,
                short.node_id,
                {"name": "no-refresh", "entrypoint": ["python", "main.py"]},
            )
        async with (
            _restart_mid_fetch(
                runtime, pg_settings, gate, patch_fresh=no_refresh
            ) as fresh,
            _http(fresh) as http2,
        ):
            job = await wait_job(http2, job_id, timeout_s=60)
        # The attempt fails typed; since V0.6 (D110) transfer_error is
        # retryable, so the job's next attempt (fresh token) succeeds.
        assert job["state"] == "succeeded", job
        first, second = job["attempts"]
        assert first["failure_type"] == "transfer_error", first
        assert "staging did not succeed within 2 s" in (first["detail"] or ""), first
        assert second["state"] == "done" and second["fencing_token"] == 2, second
    finally:
        await short.stop()
