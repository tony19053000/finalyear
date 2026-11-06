"""V1.0 G9-E tester: ``queue_reads._chunk_history`` against real PostgreSQL.

Chunk records are inserted by SQL for one fingerprint (a submitted job gives
the fingerprint; ``chunk_record`` has no foreign keys) and read back through
``_history``: median, latest ``runs``, committed-only, run_ms guards,
per-node isolation and the other-fingerprint join.
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
    token = token_of(runtime)
    async with api_client(runtime, token=token) as http:
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
    rows = await _sql(
        runtime, "SELECT id, fingerprint FROM job WHERE id = :j", j=job_id
    )
    assert rows and rows[0].fingerprint, got.text
    return job_id, str(rows[0].fingerprint)


async def _sql(runtime: ControllerRuntime, q: str, **p: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.begin() as conn:
        r = await conn.execute(text(q), p)
        return list(r.all()) if r.returns_rows else []


_n = 0


async def _chunk(
    runtime: ControllerRuntime,
    job_id: str,
    node: str,
    *,
    items: int = 100,
    run_ms: int | None = 1000,
    stage_ms: int | None = 100,
    upload_ms: int | None = 100,
    outcome: str = "committed",
    age_s: int = 0,
    ended: bool = True,
) -> None:
    global _n
    _n += 1
    await _sql(
        runtime,
        "INSERT INTO chunk_record (schema_version, job_id, chunk_index, lease_seq,"
        " lane_attempt_id, node_id, first_item, items, speculative, won, outcome,"
        " failure_class, stage_ms, run_ms, upload_ms, leased_at, ended_at)"
        " VALUES (3, :j, :i, 1, 'att', :n, 0, :items, false, true, :o, 'ok',"
        " :st, :run, :up, :t, :e)",
        j=job_id,
        i=_n,
        n=node,
        items=items,
        o=outcome,
        st=stage_ms,
        run=run_ms,
        up=upload_ms,
        t=T0,
        e=T0 + dt.timedelta(seconds=age_s) if ended else None,
    )


async def _read(runtime: ControllerRuntime, fp: str, runs: int = 3) -> Any:
    assert runtime._engine is not None
    async with AsyncSession(runtime._engine) as db:
        return (await _history(db, [fp], runs)).get(fp)


async def test_median_rate_overhead_and_exclusions(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job, fp = await _job(runtime, tmp_path, "a")
    await _chunk(runtime, job, "n1", items=100, run_ms=1000)  # 100/s
    await _chunk(runtime, job, "n1", items=100, run_ms=500, age_s=1)  # 200/s
    await _chunk(runtime, job, "n1", items=100, run_ms=250, age_s=2)  # 400/s
    # excluded rows
    await _chunk(runtime, job, "n1", outcome="failed", run_ms=1, age_s=9)
    await _chunk(runtime, job, "n1", outcome="lost", run_ms=1, age_s=9)
    await _chunk(runtime, job, "n1", outcome="superseded", run_ms=1, age_s=9)
    await _chunk(runtime, job, "n1", run_ms=0, age_s=9)  # divide by zero
    await _chunk(runtime, job, "n1", run_ms=None, age_s=9)
    await _chunk(runtime, job, "n2", items=10, run_ms=1000)  # one chunk only
    h = await _read(runtime, fp)
    assert h is not None
    assert h.item_rate_by_node["n1"] == pytest.approx(200.0)
    assert h.item_rate_samples_by_node == {"n1": 3, "n2": 1}
    assert h.item_rate_by_node["n2"] == pytest.approx(10.0)
    assert h.chunk_overhead_s == pytest.approx(0.2)
    assert h.chunk_overhead_samples == 4


async def test_only_latest_runs_chunks_count(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job, fp = await _job(runtime, tmp_path, "b")
    for i in range(5):  # old = slow (10/s), newest 2 = fast (1000/s)
        await _chunk(runtime, job, "n1", items=10, run_ms=1000, age_s=i)
    await _chunk(runtime, job, "n1", items=1000, run_ms=1000, age_s=100)
    await _chunk(runtime, job, "n1", items=1000, run_ms=1000, age_s=101)
    h = await _read(runtime, fp, runs=2)
    assert h.item_rate_samples_by_node["n1"] == 2
    assert h.item_rate_by_node["n1"] == pytest.approx(1000.0)


async def test_other_fingerprint_and_no_chunks(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job_a, fp_a = await _job(runtime, tmp_path, "c")
    _job_b, fp_b = await _job(runtime, tmp_path, "d")
    assert fp_a != fp_b
    await _chunk(runtime, job_a, "n1")
    h = await _read(runtime, fp_b)
    assert h is None or (not h.item_rate_by_node and h.chunk_overhead_s is None)
    h_a = await _read(runtime, fp_a)
    assert set(h_a.item_rate_by_node) == {"n1"}


async def test_null_ended_at_and_null_stage_do_not_crash(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job, fp = await _job(runtime, tmp_path, "e")
    await _chunk(runtime, job, "n1", ended=False, stage_ms=None, upload_ms=None)
    h = await _read(runtime, fp)
    assert h.item_rate_by_node["n1"] == pytest.approx(100.0)
    assert h.chunk_overhead_s == 0.0


async def test_huge_values_stay_finite(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    job, fp = await _job(runtime, tmp_path, "f")
    await _chunk(runtime, job, "n1", items=10**12, run_ms=1)
    h = await _read(runtime, fp)
    assert h.item_rate_by_node["n1"] == pytest.approx(1e15)
