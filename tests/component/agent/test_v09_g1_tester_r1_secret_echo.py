"""V0.9 G1 tester round 1 (RE-I6, security): a job that echoes its own secret
into results/workload.json must not get it into attempt.workload, the run
record or the admin's Parquet export - through a real controller, agent and
PostgreSQL."""

# ruff: noqa: F811
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from mf_exec_harness import ExecAgent, submit, wait_job
from sqlalchemy import text
from test_v07_g6_tester_r1_jobs import (  # noqa: F401
    alice,
    alice_token,
    settings,
)

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.telemetry.export import fetch_records, parquet_bytes

pytestmark = [
    pytest.mark.component,
    pytest.mark.security,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

CODE = """\
import json, os
os.makedirs("results", exist_ok=True)
v = os.environ["ENV_KEY"]
json.dump({"schema_version": 1, "family": "cpu.x", "status": "ok",
           "params": {"note": v, "n": 3}, "seed": 1, "device": "cpu",
           "compute_ns": 10}, open("results/workload.json", "w"))
"""


async def _echo(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    pg_settings: object,
    value: str,
) -> tuple[bytes, str]:
    assert (await alice.put("/v1/secrets/ENV_KEY", content=value.encode())).is_success
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )
    try:
        project = tmp_path / "echo"
        project.mkdir()
        (project / "main.py").write_text(CODE)
        job_id = await submit(
            alice,
            project,
            agent.node_id,
            {
                "name": "echo",
                "entrypoint": ["python", "main.py"],
                "secrets": [{"name": "ENV_KEY", "as_env": True}],
                "retry": {"max_attempts": 1},
            },
        )
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
    finally:
        await agent.stop()
    engine = create_engine(pg_settings)  # type: ignore[arg-type]
    try:
        import asyncio

        for _ in range(100):
            rows = await fetch_records(engine)
            if rows:
                break
            await asyncio.sleep(0.2)
        assert rows, "no run record"
        async with engine.connect() as conn:
            workload = await conn.scalar(text("SELECT workload::text FROM attempt"))
            tables = await conn.scalar(
                text("SELECT string_agg(r::text, '|') FROM run_record r")
            )
        return parquet_bytes(rows), f"{workload}|{tables}"
    finally:
        await engine.dispose()


async def test_an_ascii_secret_echoed_into_the_workload_report_is_dropped(
    alice: httpx.AsyncClient,
    grpc_target: str,
    runtime: ControllerRuntime,
    tmp_path: Path,
    pg_settings: object,
) -> None:
    value = "ascii-secret-value-1234567890"
    export, db = await _echo(alice, grpc_target, tmp_path, pg_settings, value)
    assert value.encode() not in export and value not in db


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-3: JSON-escaped secret (non-ASCII / quote) survives the agent's "
    "redaction of params_json",
)
@pytest.mark.parametrize(
    "value", ["pässword-é-secret-123456", 'q"uote\\back-slash-9876']
)
async def test_defect_3_a_non_ascii_secret_echoed_into_the_report_must_not_reach_the_dataset(
    alice: httpx.AsyncClient,
    grpc_target: str,
    runtime: ControllerRuntime,
    tmp_path: Path,
    pg_settings: object,
    value: str,
) -> None:
    export, db = await _echo(alice, grpc_target, tmp_path, pg_settings, value)
    import json

    escaped = json.dumps(value)[1:-1]
    for needle in (value, escaped):
        assert needle.encode() not in export, (
            f"secret in the Parquet export: {needle!r}"
        )
        assert needle not in db, f"secret in the database: {needle!r}"
