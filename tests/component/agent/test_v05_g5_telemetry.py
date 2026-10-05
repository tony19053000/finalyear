"""V0.5 G5 D93: transfer telemetry rows match what the fetcher counted.

Real controller (PostgreSQL + gRPC + REST) and two agents with peer servers. A
runs the tree first (and holds it); B runs it next and fetches from the
controller AND from A. The terminal AttemptStatus carries a TransferSummary;
the controller stores one transfer_event row per (tree, source) plus the cache
row, and ``GET /v1/jobs/{id}`` shows them - equal to B's FetchStats, byte for
byte. A replayed terminal report never doubles a row.
"""

from __future__ import annotations

import asyncio
import os
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
from mf_exec_harness import ExecAgent, wait_job
from mf_jobs_harness import token_of
from pydantic import SecretStr
from sqlalchemy import func, select

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.data import TransferEvent
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

CS = MIN_CHUNK_SIZE
PEER = {"bind": "127.0.0.1", "port": 0}


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
        **tolerant_liveness(),
    )


async def _agent(grpc_target: str, data_dir: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={
            "peer": PEER,
            "cache": {"flush_s": 0.1},
            "fetch": {"per_source": 2, "endgame_after_s": 600.0},
        },
    )


@pytest_asyncio.fixture
async def agents(
    runtime: ControllerRuntime, grpc_target: str, tmp_path: Path
) -> AsyncIterator[tuple[ExecAgent, ExecAgent]]:
    a = await _agent(grpc_target, tmp_path / "agent-a")
    b = await _agent(grpc_target, tmp_path / "agent-b")
    try:
        yield a, b
    finally:
        await b.stop()
        await a.stop()


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
        trust_env=False,
    )


async def _upload(runtime: ControllerRuntime, built: BuiltTree) -> None:
    client = ControllerClient(
        f"http://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()


async def _run(
    http: httpx.AsyncClient, built: BuiltTree, node_id: str
) -> dict[str, Any]:
    posted = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": "t", "entrypoint": ["python", "main.py"]},
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node_id},
        },
    )
    assert posted.status_code == 201, posted.text
    return await wait_job(http, str(posted.json()["job_id"]), timeout_s=120)


async def test_transfer_rows_match_the_fetch_stats_of_a_two_source_fetch(
    runtime: ControllerRuntime, agents: tuple[ExecAgent, ExecAgent], tmp_path: Path
) -> None:
    a, b = agents
    project = tmp_path / "p"
    project.mkdir()
    (project / "main.py").write_bytes(b"print(len(open('data.bin','rb').read()))\n")
    (project / "data.bin").write_bytes(os.urandom(40 * CS))
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    async with _http(runtime) as http:
        first = await _run(http, built, a.node_id)
        assert first["state"] == "succeeded", first
        assert runtime.cache_index is not None
        deadline = time.monotonic() + 30
        while not await runtime.cache_index.store.holders(
            "tree", str(built.tree.tree_id), among=[a.node_id]
        ):
            assert time.monotonic() < deadline, "A never listed as a holder"
            await asyncio.sleep(0.05)
        # One blob B already has: a cache hit row (main.py is tiny).
        assert b.executor.cas.store.put_file(project / "main.py").created
        job = await _run(http, built, b.node_id)
        assert job["state"] == "succeeded", job
        detail = (await http.get(f"/v1/jobs/{job['job_id']}")).json()
    stage = b.executor.last_stage
    assert stage is not None
    (attempt,) = detail["attempts"]
    rows = {r["source"]: r for r in attempt["transfers"]}
    tree_id = str(built.tree.tree_id)
    assert {r["tree_id"] for r in attempt["transfers"]} == {tree_id}
    assert set(rows) == {"cache", "controller", a.node_id}
    for name, src in stage.sources.items():
        row = rows[name]
        assert row["bytes"] == src.bytes, name
        assert row["units"] == src.units, name
        assert row["failures"] == src.failures, name
        assert row["duration_ms"] == round(src.duration_s * 1000), name
        assert row["dropped"] == src.dropped, name
    assert rows[a.node_id]["bytes"] > 0 and rows["controller"]["bytes"] > 0
    cache = rows["cache"]
    assert cache["units"] == stage.blobs_hit == 1
    assert cache["bytes"] == stage.bytes_hit == (project / "main.py").stat().st_size
    assert cache["resumed_chunks"] == stage.chunks_resumed
    # Exactly one row per (tree, source): a replay of the terminal report
    # (the agent re-sends it until acked) never doubles anything.
    assert runtime._engine is not None
    async with create_session_factory(runtime._engine)() as db:
        count = await db.scalar(
            select(func.count()).where(TransferEvent.job_id == job["job_id"])
        )
    assert count == 3
