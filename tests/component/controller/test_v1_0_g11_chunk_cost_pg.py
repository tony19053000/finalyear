"""V1.0 G11 (D456): per-node chunk costs from ``chunk_record`` against real
PostgreSQL, through ``queue_reads._history``.

Chunk records are inserted by SQL for one fingerprint (as the G9-E tester
does): wall time = ``ended_at - leased_at``, a lane's first chunk = its
earliest lease (any outcome), the first-chunk rows read beyond the latest
``runs`` window, too few rows -> no entry, and the D438 rate unchanged.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.tree import build_tree
from meshforge.controller.jobs.queue_reads import _history
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with (
        running_controller(g2_settings(pg_settings)) as rt,
        NodeChannels(f"127.0.0.1:{rt.grpc_port}") as channel,
    ):
        fake = FakeAgent(channel)
        await fake.connect(
            hardware=agent_pb2.HardwareSummary(
                cpu_model="t",
                cpu_cores=4,
                cpu_threads=8,
                ram_bytes=16 * 1024**3,
                capabilities=["exec", "exec-subprocess", "exec-split"],
            ),
            max_cpu_jobs=2,
        )
        fake.start_heartbeats(0.1)
        try:
            yield rt
        finally:
            await fake.close()


async def _sql(runtime: ControllerRuntime, q: str, **p: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.begin() as conn:
        r = await conn.execute(text(q), p)
        return list(r.all()) if r.returns_rows else []


async def _job(
    runtime: ControllerRuntime, tmp_path: Path, name: str
) -> tuple[str, str]:
    root = tmp_path / name
    root.mkdir()
    (root / "main.py").write_bytes(name.encode())
    built = build_tree(root)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    async with api_client(runtime, token=token_of(runtime)) as http:
        got = await http.post(
            "/v1/jobs",
            json={
                "manifest": {
                    "name": name,
                    "entrypoint": "python main.py {items_file} {out}",
                    "env": {"base": "python-cpu"},
                    "split": {"over": {"range": {"stop": 50}}},
                },
                "tree_id": str(built.tree.tree_id),
                "placement": {},
            },
        )
        assert got.status_code == 201, got.text
        job_id = str(got.json().get("job_id", ""))
    rows = await _sql(runtime, "SELECT fingerprint FROM job WHERE id = :j", j=job_id)
    assert rows and rows[0].fingerprint
    return job_id, str(rows[0].fingerprint)


_n = 0


async def _chunk(
    runtime: ControllerRuntime,
    job_id: str,
    node: str,
    lane: str,
    *,
    at_s: float,
    wall_s: float,
    items: int = 4,
    run_ms: int | None = 6000,
    stage_ms: int = 0,
    env_ms: int = 0,
    outcome: str = "committed",
) -> None:
    """One chunk try leased ``at_s`` after T0 that took ``wall_s``."""
    global _n
    _n += 1
    leased = T0 + dt.timedelta(seconds=at_s)
    await _sql(
        runtime,
        "INSERT INTO chunk_record (schema_version, job_id, chunk_index, lease_seq,"
        " lane_attempt_id, node_id, first_item, items, speculative, won, outcome,"
        " failure_class, stage_ms, env_ms, run_ms, upload_ms, leased_at, ended_at)"
        " VALUES (3, :j, :i, 1, :lane, :n, 0, :items, false, true, :o, 'ok',"
        " :st, :env, :run, 0, :t, :e)",
        j=job_id,
        i=_n,
        lane=lane,
        n=node,
        items=items,
        o=outcome,
        st=stage_ms,
        env=env_ms,
        run=run_ms,
        t=leased,
        e=leased + dt.timedelta(seconds=wall_s),
    )


async def _read(runtime: ControllerRuntime, fp: str, runs: int) -> Any:
    assert runtime._engine is not None
    async with AsyncSession(runtime._engine) as db:
        return (await _history(db, [fp], runs)).get(fp)


async def _lane(
    runtime: ControllerRuntime,
    job: str,
    node: str,
    lane: str,
    *,
    start_s: float,
    chunks: int,
    fixed_s: float,
    first_extra_s: float,
) -> None:
    """A lane: 4 items a chunk at 1.5 s/item (run 6 s), ``fixed_s`` more per
    chunk, ``first_extra_s`` more on its first chunk."""
    at = start_s
    for k in range(chunks):
        wall = 6.0 + fixed_s + (first_extra_s if k == 0 else 0.0)
        await _chunk(runtime, job, node, lane, at_s=at, wall_s=wall)
        at += wall + 0.5


async def test_fixed_and_first_chunk_costs_per_node(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job, fp = await _job(runtime, tmp_path, "a")
    await _lane(
        runtime,
        job,
        "slow",
        "lane-s",
        start_s=0,
        chunks=6,
        fixed_s=20,
        first_extra_s=15,
    )
    await _lane(
        runtime,
        job,
        "fast",
        "lane-f",
        start_s=0,
        chunks=6,
        fixed_s=0.2,
        first_extra_s=0,
    )
    h = await _read(runtime, fp, runs=20)
    assert h is not None
    assert h.item_rate_by_node["slow"] == pytest.approx(4 / 6.0)
    assert h.chunk_fixed_s_by_node["slow"] == pytest.approx(20.0)
    assert h.chunk_fixed_samples_by_node["slow"] == 5
    assert h.chunk_first_s_by_node["slow"] == pytest.approx(15.0)
    assert h.chunk_first_samples_by_node["slow"] == 1
    assert h.chunk_fixed_s_by_node["fast"] == pytest.approx(0.2)
    assert h.chunk_first_s_by_node["fast"] == pytest.approx(0.0)


async def test_first_chunks_count_beyond_the_latest_runs(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    """``runs`` = 3: the latest 3 chunks are steady; the lane's first chunk is
    older but is read as a first-chunk sample. The rate stays D438's (latest 3)."""
    job, fp = await _job(runtime, tmp_path, "b")
    await _lane(
        runtime, job, "n1", "lane-1", start_s=0, chunks=8, fixed_s=10, first_extra_s=12
    )
    h = await _read(runtime, fp, runs=3)
    assert h.item_rate_samples_by_node["n1"] == 3
    assert h.chunk_fixed_samples_by_node["n1"] == 3
    assert h.chunk_fixed_s_by_node["n1"] == pytest.approx(10.0)
    assert h.chunk_first_s_by_node["n1"] == pytest.approx(12.0)


async def test_a_failed_first_lease_makes_no_committed_first_chunk(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job, fp = await _job(runtime, tmp_path, "c")
    await _chunk(runtime, job, "n1", "lane-x", at_s=0, wall_s=40, outcome="failed")
    for k in range(4):
        await _chunk(runtime, job, "n1", "lane-x", at_s=50 + 20 * k, wall_s=16)
    h = await _read(runtime, fp, runs=20)
    assert h.chunk_fixed_s_by_node["n1"] == pytest.approx(10.0)
    assert "n1" not in h.chunk_first_s_by_node


async def test_too_few_chunks_leave_the_node_out(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job, fp = await _job(runtime, tmp_path, "d")
    await _lane(
        runtime, job, "n1", "lane-1", start_s=0, chunks=3, fixed_s=10, first_extra_s=5
    )
    h = await _read(runtime, fp, runs=20)
    assert "n1" in h.item_rate_by_node  # the rate is still read
    assert h.chunk_fixed_s_by_node == {}
    assert h.chunk_first_s_by_node == {}
