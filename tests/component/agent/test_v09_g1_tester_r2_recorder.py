"""V0.9 G1 tester round 2: attacks on the recorder's row isolation (fix r1) on real
PostgreSQL + a real controller + a real agent. The poison is injected past the
builder so the recorder's own last line of defence is what is tested.
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


async def _run_jobs(
    http: httpx.AsyncClient, agent: ExecAgent, tmp_path: Path, n: int
) -> list[str]:
    ids = []
    for i in range(n):
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
    return ids


async def _reset(runtime: ControllerRuntime, engine: Any, want: int) -> None:
    await _wait_records(engine, want)
    assert runtime.recorder is not None

    async def never() -> int:
        return 0

    runtime.recorder.sweep = never  # type: ignore[method-assign]
    await asyncio.sleep(0.5)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))
        await conn.execute(text("UPDATE attempt SET recorded = false"))  # F-4
        await conn.execute(
            text("DELETE FROM audit_event WHERE action = 'telemetry_record_skipped'")
        )


def _poison(
    monkeypatch: pytest.MonkeyPatch, victims: set[str], value: object = "d" * 100
) -> None:
    real = recorder_module.build_record  # type: ignore[attr-defined]

    def poisoned(source: Any) -> dict[str, Any]:
        record = real(source)
        if source.job_id in victims:
            record["p_dtype"] = value
        return record

    monkeypatch.setattr(recorder_module, "build_record", poisoned)


async def _api_skipped(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await http.get("/v1/alerts", params={"limit": 100})
    assert response.status_code == 200, response.text
    return [
        a for a in response.json()["alerts"] if a["kind"] == "telemetry_record_skipped"
    ]


async def test_a_mixed_batch_writes_every_good_row_and_alerts_each_bad_one_via_the_api(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await _run_jobs(http, agent, tmp_path, 6)
    await _reset(runtime, engine, 6)
    victims = {ids[0], ids[1], ids[5]}  # first, adjacent pair, and last
    _poison(monkeypatch, victims)
    rec = RunRecorder(engine)
    assert await rec.sweep() == 3
    assert {r["job_id"] for r in await fetch_records(engine)} == set(ids) - victims
    assert rec.skipped == 3 and len(rec.skipped_attempts) == 3
    skipped = await _api_skipped(http)
    assert {a["details"]["job_id"] for a in skipped} == victims
    assert all(a["actor"] == "controller" for a in skipped)
    # idempotent: sweeping again adds neither records nor alerts
    assert await rec.sweep() == 0
    assert len(await _api_skipped(http)) == 3
    # the alert details do not carry the refused value
    assert "ddddd" not in str(skipped)


async def test_a_batch_of_only_bad_rows_writes_nothing_and_does_not_raise(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await _run_jobs(http, agent, tmp_path, 3)
    await _reset(runtime, engine, 3)
    _poison(monkeypatch, set(ids))
    rec = RunRecorder(engine)
    assert await rec.sweep() == 0
    assert rec.skipped == 3
    assert await fetch_records(engine) == []
    assert len(await _api_skipped(http)) == 3


async def test_a_batch_refusal_that_no_single_row_reproduces_skips_nothing(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The multi-row INSERT is refused once (a data error), but every row on its
    own is fine: all are written and none is skipped or alerted."""
    from sqlalchemy.exc import DataError

    await _run_jobs(http, agent, tmp_path, 3)
    await _reset(runtime, engine, 3)
    real = recorder_module._insert_rows
    first = True

    async def once(db: Any, records: Any) -> int:
        nonlocal first
        if first and len(records) > 1:
            first = False

            class OrigError(Exception):
                sqlstate = "22001"

            raise DataError("INSERT", {}, OrigError("too long"))
        return await real(db, records)

    monkeypatch.setattr(recorder_module, "_insert_rows", once)
    rec = RunRecorder(engine)
    assert await rec.sweep() == 3
    assert rec.skipped == 0
    assert await _api_skipped(http) == []


@pytest.mark.parametrize(
    "sqlstate", ["08006", "40001", "57P01", "53100", "XX000", None]
)
async def test_non_data_sqlstates_are_never_skipped(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str | None,
) -> None:
    from sqlalchemy.exc import DBAPIError

    await _run_jobs(http, agent, tmp_path, 2)
    await _reset(runtime, engine, 2)

    class OrigError(Exception):
        pass

    OrigError.sqlstate = sqlstate  # type: ignore[attr-defined]

    async def boom(db: Any, records: Any) -> int:
        raise DBAPIError("INSERT", {}, OrigError("x"))

    monkeypatch.setattr(recorder_module, "_insert_rows", boom)
    rec = RunRecorder(engine)
    with pytest.raises(recorder_module.TelemetryStoreError):
        await rec.sweep()
    assert rec.skipped == 0 and not rec.skipped_attempts
    assert await _api_skipped(http) == []
    assert await fetch_records(engine) == []


async def test_the_real_backend_dying_mid_row_by_row_rolls_everything_back(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await _run_jobs(http, agent, tmp_path, 3)
    await _reset(runtime, engine, 3)
    _poison(monkeypatch, {ids[0]})
    real = recorder_module._insert_rows
    killed = False

    async def kill_on_second_row(db: Any, records: Any) -> int:
        nonlocal killed
        if len(records) == 1 and records[0]["job_id"] != ids[0] and not killed:
            killed = True
            await db.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
        return await real(db, records)

    monkeypatch.setattr(recorder_module, "_insert_rows", kill_on_second_row)
    rec = RunRecorder(engine)
    with pytest.raises(recorder_module.TelemetryStoreError):
        await rec.sweep()
    assert killed
    assert rec.skipped == 0 and not rec.skipped_attempts and rec.written == 0
    assert await fetch_records(engine) == []
    assert await _api_skipped(http) == []
    monkeypatch.setattr(recorder_module, "_insert_rows", real)
    assert await rec.sweep() == 2  # the engine recovers; bad row skipped + alerted
    assert rec.skipped == 1 and len(await _api_skipped(http)) == 1


async def test_a_skip_survives_only_in_the_process_that_committed_it(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two recorders (two controllers) sweep the same backlog concurrently:
    exactly one record per attempt, no duplicate rows, no crash."""
    ids = await _run_jobs(http, agent, tmp_path, 5)
    await _reset(runtime, engine, 5)
    a, b = RunRecorder(engine), RunRecorder(engine)
    results = await asyncio.gather(a.sweep(), b.sweep(), return_exceptions=True)
    assert all(isinstance(r, int) for r in results), results
    rows = await fetch_records(engine)
    assert sorted(r["job_id"] for r in rows) == sorted(ids)


async def test_defect_t2_1_postgres_jsonb_refuses_infinity_so_the_controller_must(
    engine: Any,
) -> None:
    """Pins the premise of the unit test ``test_defect_t2_1_*``: JSONB cannot
    hold the Infinity that ``parse_status`` lets through."""
    from sqlalchemy import literal, select
    from sqlalchemy.exc import DBAPIError, StatementError

    from meshforge.controller.store.models.job import Attempt

    jtype = Attempt.__table__.c.workload.type
    async with engine.connect() as conn:
        with pytest.raises((DBAPIError, StatementError)):
            await conn.execute(select(literal({"a": float("inf")}, type_=jtype)))
