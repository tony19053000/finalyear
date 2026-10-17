"""V0.9 G1 fix round 1 (T-2, T-5) on real PostgreSQL + a real controller + a
real agent: a record PostgreSQL refuses is isolated (the rest of the batch is
written), counted, and raised as a ``telemetry_record_skipped`` alert; it is
retried after a restart.

The builder already bounds untrusted values, so the refusal is forced here by
wrapping the builder to emit a value no column can hold - this exercises the
recorder's own isolation, the last line of defence.
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import text

from meshforge.controller.alerts import AlertLog
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.telemetry import recorder as recorder_module
from meshforge.controller.telemetry.export import fetch_records
from meshforge.controller.telemetry.recorder import RunRecorder

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

CODE = "print('hi')\n"


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


@pytest_asyncio.fixture
async def engine(pg_settings: Any) -> AsyncIterator[Any]:
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _wait_records(engine: Any, want: int, within: float = 20.0) -> list[Any]:
    deadline = time.monotonic() + within
    while True:
        rows = await fetch_records(engine)
        if len(rows) >= want or time.monotonic() > deadline:
            return rows
        await asyncio.sleep(0.2)


@pytest.mark.parametrize(
    ("column", "value"),
    [("p_dtype", "d" * 100), ("workload_params", {"a": "x\u0000y"})],
    ids=["varchar-overflow", "jsonb-nul"],
)
async def test_a_refused_record_is_isolated_counted_alerted_and_retried(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: object,
) -> None:
    ids = []
    for i in range(3):
        project = tmp_path / f"p{i}"
        project.mkdir()
        (project / "main.py").write_text(CODE)
        job_id = await submit(
            http,
            project,
            agent.node_id,
            {"name": f"p{i}", "entrypoint": ["python", "main.py"]},
        )
        await wait_job(http, job_id, timeout_s=40)
        ids.append(job_id)
    await _wait_records(engine, 3)
    assert runtime.recorder is not None

    async def never() -> int:
        return 0

    runtime.recorder.sweep = never  # type: ignore[method-assign]
    await asyncio.sleep(0.5)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))
        await conn.execute(
            text("DELETE FROM audit_event WHERE action = 'telemetry_record_skipped'")
        )

    victim = ids[1]
    real = recorder_module.build_record

    def poisoned(source: Any) -> dict[str, Any]:
        record = real(source)
        if source.job_id == victim:
            record[column] = value  # what PostgreSQL refuses (SQLSTATE 22xxx)
        return record

    monkeypatch.setattr(recorder_module, "build_record", poisoned)
    rec = RunRecorder(engine)
    assert await rec.sweep() == 2  # the batch is not lost with the bad row
    assert {r["job_id"] for r in await fetch_records(engine)} == set(ids) - {victim}
    assert rec.skipped == 1 and len(rec.skipped_attempts) == 1
    assert await rec.sweep() == 0  # skipped, nothing spins

    alerts = await AlertLog(engine).list(limit=10)
    skipped = [a for a in alerts if a.kind == "telemetry_record_skipped"]
    assert len(skipped) == 1
    details = skipped[0].details
    assert details["job_id"] == victim and details["reason"] == "refused"
    assert "22" in details["error_type"]  # the SQLSTATE class, for the operator
    assert set(details) == {"attempt_id", "job_id", "reason", "error_type"}

    monkeypatch.setattr(recorder_module, "build_record", real)
    assert await RunRecorder(engine).sweep() == 1  # restart -> retried, written
    assert len(await fetch_records(engine)) == 3


async def test_a_database_outage_is_not_mistaken_for_a_refused_record(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-data failure (a lost connection, no SQLSTATE) fails the sweep as
    before - the next sweep retries everything; nothing is skipped."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "main.py").write_text(CODE)
    job_id = await submit(
        http, project, agent.node_id, {"name": "p", "entrypoint": ["python", "main.py"]}
    )
    await wait_job(http, job_id, timeout_s=40)
    await _wait_records(engine, 1)
    assert runtime.recorder is not None

    async def never() -> int:
        return 0

    runtime.recorder.sweep = never  # type: ignore[method-assign]
    await asyncio.sleep(0.5)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))

    from sqlalchemy.exc import OperationalError

    real = recorder_module._insert_rows

    async def outage(db: Any, records: Any) -> int:
        raise OperationalError("INSERT", {}, ConnectionResetError("gone"))

    monkeypatch.setattr(recorder_module, "_insert_rows", outage)
    rec = RunRecorder(engine)
    with pytest.raises(recorder_module.TelemetryStoreError):
        await rec.sweep()
    assert rec.skipped == 0 and not rec.skipped_attempts
    monkeypatch.setattr(recorder_module, "_insert_rows", real)
    assert await rec.sweep() == 1


async def test_a_skip_in_a_sweep_that_rolls_back_is_not_kept_without_its_alert(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused row is skipped, then the same sweep loses the database: the
    transaction (and the skip's alert row) rolls back, so the skip must not
    stick - the next sweep skips it again *with* its alert."""
    ids = []
    for i in range(2):
        project = tmp_path / f"p{i}"
        project.mkdir()
        (project / "main.py").write_text(CODE)
        job_id = await submit(
            http,
            project,
            agent.node_id,
            {"name": f"p{i}", "entrypoint": ["python", "main.py"]},
        )
        await wait_job(http, job_id, timeout_s=40)
        ids.append(job_id)
    await _wait_records(engine, 2)
    assert runtime.recorder is not None

    async def never() -> int:
        return 0

    runtime.recorder.sweep = never  # type: ignore[method-assign]
    await asyncio.sleep(0.5)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))
        await conn.execute(
            text("DELETE FROM audit_event WHERE action = 'telemetry_record_skipped'")
        )

    victim = ids[0]  # first in sweep order: skipped before the outage hits
    real_build = recorder_module.build_record

    def poisoned(source: Any) -> dict[str, Any]:
        record = real_build(source)
        if source.job_id == victim:
            record["p_dtype"] = "d" * 100  # VARCHAR(64): SQLSTATE 22001
        return record

    from sqlalchemy.exc import OperationalError

    real_insert = recorder_module._insert_rows
    down = True

    async def flaky(db: Any, records: Any) -> int:
        if down and len(records) == 1 and records[0]["job_id"] != victim:
            raise OperationalError("INSERT", {}, ConnectionResetError("gone"))
        return await real_insert(db, records)

    monkeypatch.setattr(recorder_module, "build_record", poisoned)
    monkeypatch.setattr(recorder_module, "_insert_rows", flaky)
    rec = RunRecorder(engine)
    with pytest.raises(recorder_module.TelemetryStoreError):
        await rec.sweep()
    assert rec.skipped == 0 and not rec.skipped_attempts
    alerts = await AlertLog(engine).list(limit=10)
    assert not [a for a in alerts if a.kind == "telemetry_record_skipped"]

    down = False
    assert await rec.sweep() == 1
    assert rec.skipped == 1 and len(rec.skipped_attempts) == 1
    alerts = await AlertLog(engine).list(limit=10)
    skipped = [a for a in alerts if a.kind == "telemetry_record_skipped"]
    assert len(skipped) == 1 and skipped[0].details["job_id"] == victim
