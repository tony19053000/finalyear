"""V0.9 G1 items 2-5 on real PostgreSQL + a real controller + a real agent.

Real attempts (never hand-built samples - the f6fe6f9 lesson): a normal job
that succeeds, one that fails, and a benchmark-shaped job whose workload writes
``results/workload.json`` (``compute_ns``, exit 3 = unavailable). Each terminal
attempt gets exactly one run record from the recorder sweep; the records carry
the start snapshot, the phases, the compute time, the clock offset and a
failure class; the Parquet export of them is byte-identical twice.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
from sqlalchemy import func, select

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.export import fetch_records, parquet_bytes
from meshforge.controller.telemetry.schema import COLUMNS

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

BENCH_CODE = """\
import json, os, sys, time
os.makedirs("results", exist_ok=True)
begin = time.perf_counter_ns()
total = sum(i * i for i in range(200_000))
compute_ns = time.perf_counter_ns() - begin
status = sys.argv[1]
with open("results/workload.json", "w") as fh:
    json.dump({"schema_version": 1, "family": "cpu.sumsq", "params": {"n": 200000},
               "seed": 3, "status": status, "device": "cpu",
               "compute_ns": compute_ns, "verified": status == "ok"}, fh)
print("total", total, flush=True)
sys.exit({"ok": 0, "unavailable": 3, "check_failed": 4}[status])
"""
PLAIN_CODE = "import sys\nprint('hi')\nsys.exit(int(sys.argv[1]))\n"


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        telemetry={"record_interval_s": 0.2},
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
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


def _project(tmp_path: Path, name: str, code: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(code)
    return project


async def _records(
    pg_settings: Any, want: int, within: float = 30.0
) -> list[dict[str, Any]]:
    engine = create_engine(pg_settings)
    try:
        deadline = time.monotonic() + within
        while True:
            rows = await fetch_records(engine)
            if len(rows) >= want or time.monotonic() > deadline:
                return rows
            await asyncio.sleep(0.2)
    finally:
        await engine.dispose()


async def test_every_real_attempt_gets_one_complete_record_and_exports_reproducibly(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    pg_settings: Any,
    tmp_path: Path,
) -> None:
    # Let a few Ping/Pong exchanges happen so the clock estimate exists.
    await asyncio.sleep(0.5)
    jobs: dict[str, str] = {}
    for name, code, args in (
        ("ok", PLAIN_CODE, ["0"]),
        ("fails", PLAIN_CODE, ["1"]),
        ("bench_ok", BENCH_CODE, ["ok"]),
        ("bench_unavailable", BENCH_CODE, ["unavailable"]),
    ):
        manifest = {
            "name": name.replace("_", "-"),
            "entrypoint": ["python", "main.py", *args],
            "retry": {"max_attempts": 1},
        }
        jobs[name] = await submit(
            http, _project(tmp_path, name, code), agent.node_id, manifest
        )
    for job_id in jobs.values():
        await wait_job(http, job_id, timeout_s=60)
    rows = await _records(pg_settings, len(jobs))
    by_job = {r["job_id"]: r for r in rows}
    assert set(by_job) == set(jobs.values()), rows
    ok, fails = by_job[jobs["ok"]], by_job[jobs["fails"]]
    bench, unavailable = by_job[jobs["bench_ok"]], by_job[jobs["bench_unavailable"]]

    for record in rows:
        assert record["schema_version"] == 1
        assert record["node_id"] == agent.node_id
        assert record["placement_mode"] == "pinned" and record["strategy"] == "mode1"
        assert record["run_ms"] is not None and record["queue_ms"] is not None
        assert record["node_state"] == "online"
        assert record["node_live_attempts"] is not None
        assert record["cache_inputs"] == 1  # the bundle
        # one host: the measured offset is ~0 and within its bound
        assert record["clock_offset_ms"] is not None, record
        assert abs(record["clock_offset_ms"]) <= record["clock_offset_bound_ms"] + 1.0
        assert record["recorded_at"] is not None
    assert (ok["failure_class"], ok["attempt_state"]) == ("ok", "done")
    assert ok["workload_family"] is None and ok["compute_ns"] is None
    assert (fails["failure_class"], fails["failure_type"]) == ("workload", "user_error")
    assert bench["failure_class"] == "ok" and bench["workload_family"] == "cpu.sumsq"
    assert bench["p_n"] == 200000 and bench["workload_seed"] == 3
    assert bench["compute_ns"] is not None and bench["compute_ns"] > 0
    assert bench["compute_ns"] <= bench["run_ms"] * 1_000_000  # compute inside run
    assert unavailable["failure_class"] == "unavailable"
    assert unavailable["exit_code"] == 3

    # exactly one record per attempt, even after more sweeps
    assert runtime.recorder is not None
    await runtime.recorder.sweep()
    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            count = await conn.scalar(select(func.count()).select_from(RunRecord))
        assert count == len(jobs)
        first = parquet_bytes(await fetch_records(engine))
        second = parquet_bytes(await fetch_records(engine))
    finally:
        await engine.dispose()
    assert first == second

    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(first))
    assert table.column_names == [c.name for c in COLUMNS]
    assert table.num_rows == len(jobs)
    params = [p for p in table.column("workload_params").to_pylist() if p]
    assert json.loads(params[0]) == {"n": 200000}
