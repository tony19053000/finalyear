"""V0.6 G1 / D114 (0.6.14): a controller restart in the middle of an output upload.

Real PostgreSQL + gRPC + REST + a real agent (subprocess runtime). The workload
writes an incompressible output several parts long; the agent uploads it in
64 KiB parts. The agent's read of one part is held at a gate until the
controller is stopped, so the next request meets a dead port (connection
refused). A fresh :class:`ControllerRuntime` comes up on the same ports,
database and blob directory: its grant table is empty (the old grant is
refused, 401), and after the agent's Reconcile it sends ``RefreshUploadGrant``.

Checked: the job SUCCEEDS with the output byte-identical; every part is sent
exactly once (the parts stored before the restart are not sent again, counted
on both sides); the pre-restart grant is refused by the new controller; with no
refresh coming, the upload gives up at ``upload_retry_s`` as ``output_error``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
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
from mf_jobs_harness import token_of

from meshforge.agent.execution import executor as executor_module
from meshforge.agent.execution import uploader
from meshforge.agent.execution.uploader import UploadProgress
from meshforge.common.bundle import unpack
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(150)]

PART = MIN_CHUNK_SIZE
OUTPUT_BYTES = 12 * PART + 1234
HOLD_AT_PART = 6
"""Part reads 1..5 are sent (and stored) before the restart; the 6th is held."""

WRITER = """\
import os, sys
with open("out.bin", "wb") as fh:
    fh.write(os.urandom(int(sys.argv[1])))
"""


def _settings(pg: DatabaseSettings, **binds: str) -> ControllerSettings:
    return make_controller_settings(
        pg,
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),  # not a liveness test
        **binds,
    )


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return _settings(pg_settings)


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    )


class PartGate:
    """Holds the n-th part read of the uploader (a worker thread) until the
    test releases it; records the grants and progress the uploads used."""

    def __init__(self, at: int) -> None:
        self.at = at
        self.reads = 0
        self.loop = asyncio.get_running_loop()
        self.reached = asyncio.Event()
        self.release = threading.Event()
        self.tokens: list[str] = []
        self.progress: list[UploadProgress] = []
        self._real_read = uploader._read_part
        self._real_upload = executor_module.upload_parts

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def read(path: Path, start: int, length: int) -> bytes:
            self.reads += 1
            if self.reads == self.at:
                self.loop.call_soon_threadsafe(self.reached.set)
                self.release.wait(60)
            return self._real_read(path, start, length)

        async def upload(
            base: str, digest: Any, token: str, path: Path, **kwargs: Any
        ) -> int:
            self.tokens.append(token)
            progress = kwargs.get("progress")
            if isinstance(progress, UploadProgress) and progress not in self.progress:
                self.progress.append(progress)
            return await self._real_upload(base, digest, token, path, **kwargs)

        monkeypatch.setattr(uploader, "_read_part", read)
        monkeypatch.setattr(executor_module, "upload_parts", upload)


@contextlib.asynccontextmanager
async def _restart_mid_upload(
    runtime: ControllerRuntime,
    pg: DatabaseSettings,
    gate: PartGate,
    *,
    patch_fresh: Callable[[ControllerRuntime], None] | None = None,
) -> AsyncIterator[ControllerRuntime]:
    try:
        await asyncio.wait_for(gate.reached.wait(), 60)
        grpc_port, http_port = runtime.grpc_port, runtime.http_port
        assert runtime.blobs is not None
        stored_before = runtime.blobs.parts_received
        await runtime.stop()
        gate.release.set()  # the held part now meets a dead port
        await asyncio.sleep(0.5)
        fresh_settings = _settings(
            pg, grpc_bind=f"127.0.0.1:{grpc_port}", http_bind=f"127.0.0.1:{http_port}"
        )
        async with ControllerRuntime.start(fresh_settings) as fresh:
            fresh.stored_before = stored_before  # type: ignore[attr-defined]
            if patch_fresh is not None:
                patch_fresh(fresh)
            yield fresh
    finally:
        gate.release.set()


async def _submit(runtime: ControllerRuntime, agent: ExecAgent, tmp_path: Path) -> str:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WRITER)
    async with _http(runtime) as http:
        return await submit(
            http,
            project,
            agent.node_id,
            {
                "name": "upload-restart",
                "entrypoint": ["python", "main.py", str(OUTPUT_BYTES)],
                "outputs": ["out.bin"],
            },
        )


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(
        grpc_target,
        data_dir,
        execution={"upload_retry_s": 60, "upload_part_bytes": PART},
    )
    yield running
    await running.stop()


async def test_output_upload_survives_a_controller_restart_and_resumes(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    agent: ExecAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = PartGate(HOLD_AT_PART)
    gate.install(monkeypatch)
    job_id = await _submit(runtime, agent, tmp_path)
    async with (
        _restart_mid_upload(runtime, pg_settings, gate) as fresh,
        _http(fresh) as http2,
    ):
        job = await wait_job(http2, job_id, timeout_s=90)
        assert job["state"] == "succeeded", job
        (attempt,) = job["attempts"]
        digest = str(attempt["output_digest"]).removeprefix("sha256:")
        got = await http2.get(f"/v1/blobs/{digest}")
        assert got.status_code == 200
        assert hashlib.sha256(got.content).hexdigest() == digest
        archive = tmp_path / "got.bundle"
        archive.write_bytes(got.content)
        unpack(archive, tmp_path / "got")
        assert (tmp_path / "got" / "out.bin").stat().st_size == OUTPUT_BYTES

        # The grant the agent used before the restart means nothing here now.
        old = gate.tokens[0]
        async with httpx.AsyncClient(timeout=10) as raw:
            refused = await raw.get(
                f"http://127.0.0.1:{fresh.http_port}/v1/blobs/{digest}/parts",
                headers={"authorization": f"Bearer {old}"},
            )
        assert refused.status_code == 401, refused.status_code
        assert fresh.dispatcher is not None
        assert fresh.dispatcher.stats.upload_grants_refreshed == 1
        assert gate.tokens[-1] != old  # the resumed upload used the new grant

        # Counted on the controller: the old process stored HOLD_AT_PART - 1
        # parts, the new one exactly the rest - nothing twice.
        archive_size = len(got.content)
        count = -(-archive_size // PART)
        stored_before = fresh.stored_before  # type: ignore[attr-defined]
        assert fresh.blobs is not None
        assert stored_before == HOLD_AT_PART - 1
        assert fresh.blobs.parts_received == count - stored_before

    # Counted on the agent: each part index sent once, the stored ones skipped.
    (progress,) = gate.progress
    assert sorted(progress.sent_indices) == list(range(count))
    assert len(progress.sent_indices) == count
    assert progress.parts_skipped == HOLD_AT_PART - 1
    assert progress.whole_puts == 0
    # Part bytes only (the logs went up with whole PUTs before the outputs).
    assert sum(
        min(PART, archive_size - i * PART) for i in progress.sent_indices
    ) == archive_size


async def test_no_fresh_grant_within_upload_retry_s_is_output_error(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent-short",
        execution={"upload_retry_s": 2, "upload_part_bytes": PART},
    )
    try:
        gate = PartGate(HOLD_AT_PART)
        gate.install(monkeypatch)

        def no_refresh(fresh: ControllerRuntime) -> None:
            assert fresh.dispatcher is not None

            async def noop(*_a: object, **_k: object) -> None:
                return None

            fresh.dispatcher._refresh_upload_grant = noop  # type: ignore[method-assign]

        job_id = await _submit(runtime, short, tmp_path)
        async with (
            _restart_mid_upload(
                runtime, pg_settings, gate, patch_fresh=no_refresh
            ) as fresh,
            _http(fresh) as http2,
        ):
            job = await wait_job(http2, job_id, timeout_s=60)
        assert job["state"] == "failed", job
        assert job["failure_type"] == "output_error", job
        (attempt,) = job["attempts"]
        assert "upload did not succeed within 2 s" in (attempt["detail"] or ""), (
            attempt
        )
    finally:
        await short.stop()
