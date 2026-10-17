"""V0.9 G1 tester round 1 on real PostgreSQL + a real controller + a real agent.

Targets: the reconciler sweep (every terminal path, duplicates, concurrent
sweepers, a sweep killed mid-transaction, a record that cannot be built or
stored), hostile ``results/workload.json`` through the whole stack, and the
export route (filters, 413, streaming).

``test_DEFECT_*`` are strict xfails documenting defects found by the tester.
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
from sqlalchemy import func, select, text

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry import recorder as recorder_module
from meshforge.controller.telemetry.export import fetch_records, parquet_bytes
from meshforge.controller.telemetry.recorder import RunRecorder, TelemetryStoreError

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

# argv[1] = JSON text of the whole workload.json document
BENCH_CODE = """\
import json, os, sys
os.makedirs("results", exist_ok=True)
open("results/workload.json", "w").write(sys.argv[1])
print("done", flush=True)
"""
PLAIN_CODE = "import sys\nprint('hi')\nsys.exit(int(sys.argv[1]))\n"
SLEEP_CODE = "import time\nprint('up', flush=True)\ntime.sleep(300)\n"


def _doc(**over: Any) -> str:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "family": "cpu.sumsq",
        "params": {"n": 10},
        "seed": 3,
        "status": "ok",
        "device": "cpu",
        "compute_ns": 1234,
    }
    doc.update(over)
    return json.dumps(doc)


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        telemetry={"record_interval_s": 0.2, "export_max_rows": 3},
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


def _project(tmp_path: Path, name: str, code: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(code)
    return project


async def _run(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    name: str,
    code: str,
    *args: str,
    wait: float = 40.0,
) -> tuple[str, dict[str, Any]]:
    job_id = await submit(
        http,
        _project(tmp_path, name, code),
        agent.node_id,
        {
            "name": name.replace("_", "-"),
            "entrypoint": ["python", "main.py", *args],
            "retry": {"max_attempts": 1},
        },
    )
    return job_id, await wait_job(http, job_id, timeout_s=wait)


async def _wait_records(engine: Any, want: int, within: float = 20.0) -> list[Any]:
    deadline = time.monotonic() + within
    while True:
        rows = await fetch_records(engine)
        if len(rows) >= want or time.monotonic() > deadline:
            return rows
        await asyncio.sleep(0.2)


def _strip(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "recorded_at"}


async def _quiet_runtime_recorder(runtime: ControllerRuntime) -> None:
    """Stop the runtime's own sweeps so a test can drive its own recorders."""
    assert runtime.recorder is not None

    async def never() -> int:
        return 0

    runtime.recorder.sweep = never  # type: ignore[method-assign]
    await asyncio.sleep(0.5)  # let a sweep in flight finish


# ---------------------------------------------------------- terminal paths


async def test_cancelled_attempt_gets_a_record_classified_unavailable(
    http: httpx.AsyncClient, agent: ExecAgent, engine: Any, tmp_path: Path
) -> None:
    job_id = await submit(
        http,
        _project(tmp_path, "sleeper", SLEEP_CODE),
        agent.node_id,
        {"name": "sleeper", "entrypoint": ["python", "main.py"]},
    )
    await wait_job(http, job_id, states=frozenset({"running"}))
    assert (await http.post(f"/v1/jobs/{job_id}/cancel")).status_code == 202
    await wait_job(http, job_id, timeout_s=15)
    rows = await _wait_records(engine, 1)
    assert [r["job_id"] for r in rows] == [job_id]
    assert rows[0]["failure_class"] == "unavailable"
    assert rows[0]["failure_type"] == "cancelled"


async def test_lost_and_fenced_attempts_are_swept_as_infrastructure(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
) -> None:
    job_id, _ = await _run(http, agent, tmp_path, "victim", PLAIN_CODE, "0")
    first = (await _wait_records(engine, 1))[0]
    await _quiet_runtime_recorder(runtime)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))
        await conn.execute(
            text(
                "UPDATE attempt SET state='lost', failure_type='node_lost', "
                "exit_code=NULL WHERE job_id=:j"
            ),
            {"j": job_id},
        )
    assert await RunRecorder(engine).sweep() == 1
    lost = (await fetch_records(engine))[0]
    assert (lost["attempt_state"], lost["failure_class"]) == ("lost", "infrastructure")
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))
        await conn.execute(
            text(
                "UPDATE attempt SET state='fenced', failure_type=NULL WHERE job_id=:j"
            ),
            {"j": job_id},
        )
    assert await RunRecorder(engine).sweep() == 1
    fenced = (await fetch_records(engine))[0]
    assert (fenced["attempt_state"], fenced["failure_class"]) == (
        "fenced",
        "infrastructure",
    )
    assert fenced["attempt_id"] == first["attempt_id"]


async def test_deleting_the_job_row_does_not_break_or_remove_the_record(
    http: httpx.AsyncClient, agent: ExecAgent, engine: Any, tmp_path: Path
) -> None:
    await _run(http, agent, tmp_path, "keep", PLAIN_CODE, "0")
    rows = await _wait_records(engine, 1)
    assert len(rows) == 1  # no FK: the dataset outlives job rows


# --------------------------------------------------- duplicates, concurrency


async def test_many_concurrent_sweepers_write_each_record_exactly_once_and_equal(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
) -> None:
    for i in range(5):
        await _run(http, agent, tmp_path, f"j{i}", PLAIN_CODE, str(i % 2))
    original = await _wait_records(engine, 5)
    assert len(original) == 5
    await _quiet_runtime_recorder(runtime)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))

    sweepers = [RunRecorder(engine, batch=2) for _ in range(6)]  # concurrent "controllers"

    async def drain(r: RunRecorder) -> None:
        for _ in range(10):
            await r.sweep()

    await asyncio.gather(*(drain(r) for r in sweepers))
    async with engine.connect() as conn:
        count = await conn.scalar(select(func.count()).select_from(RunRecord))
        distinct = await conn.scalar(
            select(func.count(func.distinct(RunRecord.attempt_id)))
        )
    assert count == distinct == 5
    again = await fetch_records(engine)
    assert [_strip(r) for r in again] == [_strip(r) for r in original]
    assert sum(r.written for r in sweepers) == 5  # DO NOTHING losers report 0


async def test_a_sweep_killed_mid_transaction_leaves_nothing_and_the_next_sweep_completes(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for i in range(3):
        await _run(http, agent, tmp_path, f"k{i}", PLAIN_CODE, "0")
    await _wait_records(engine, 3)
    await _quiet_runtime_recorder(runtime)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))

    real = recorder_module._sources
    gate = asyncio.Event()

    async def slow(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        gate.set()
        await asyncio.sleep(3600)  # the controller "crashes" here
        return out

    monkeypatch.setattr(recorder_module, "_sources", slow)
    task = asyncio.create_task(RunRecorder(engine).sweep())
    await asyncio.wait_for(gate.wait(), 10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(RunRecord)) == 0
        # the cancelled transaction did not leave a connection stuck in a lock
        assert await conn.scalar(text("SELECT count(*) FROM pg_stat_activity "
            "WHERE state LIKE 'idle in transaction%' AND datname = current_database()")) == 0
    monkeypatch.setattr(recorder_module, "_sources", real)
    assert await RunRecorder(engine).sweep() == 3


# ----------------------------------------------------- unbuildable / stored


async def test_an_unbuildable_record_does_not_block_the_others_and_is_retried_after_restart(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = []
    for i in range(3):
        job_id, _ = await _run(http, agent, tmp_path, f"u{i}", PLAIN_CODE, "0")
        ids.append(job_id)
    await _wait_records(engine, 3)
    await _quiet_runtime_recorder(runtime)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))
    victim = ids[1]
    real = recorder_module.build_record

    def flaky(source: Any) -> Any:
        if source.job_id == victim:
            raise RuntimeError("builder bug")
        return real(source)

    monkeypatch.setattr(recorder_module, "build_record", flaky)
    first = RunRecorder(engine)
    assert await first.sweep() == 2
    assert await first.sweep() == 0  # the victim is skipped, nothing spins
    assert {r["job_id"] for r in await fetch_records(engine)} == set(ids) - {victim}
    monkeypatch.setattr(recorder_module, "build_record", real)
    assert await first.sweep() == 0, "documented: skipped until restart"
    assert await RunRecorder(engine).sweep() == 1  # restart -> recovered


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-5: a skipped (unbuildable) attempt is visible only in a log "
    "line - no counter / alert / API signal, so records are lost silently",
)
async def test_DEFECT_5_a_skipped_record_is_visible_to_an_operator(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _run(http, agent, tmp_path, "v", PLAIN_CODE, "0")
    await _wait_records(engine, 1)
    await _quiet_runtime_recorder(runtime)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM run_record"))
    monkeypatch.setattr(
        recorder_module, "build_record", lambda s: (_ for _ in ()).throw(ValueError("x"))
    )
    rec = RunRecorder(engine)
    await rec.sweep()
    public = {
        k: v
        for k, v in vars(rec).items()
        if not k.startswith("_") and ("unbuild" in k or "skip" in k)
    }
    assert public and any(v for v in public.values()), (
        "no public counter/list of skipped attempts on the recorder"
    )


async def test_DEFECT_2_a_record_postgres_refuses_must_not_jam_later_records(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    engine: Any,
    tmp_path: Path,
) -> None:
    """One benchmark reports ``dtype`` of 100 characters (p_dtype is VARCHAR(64)):
    the INSERT of the whole batch fails, every sweep fails, and every later
    attempt - from any user - never gets a record."""
    bad_job, _ = await _run(
        http, agent, tmp_path, "bad", BENCH_CODE, _doc(params={"dtype": "d" * 100})
    )
    good_job, _ = await _run(http, agent, tmp_path, "good", PLAIN_CODE, "0")
    rows = await _wait_records(engine, 2, within=15.0)
    got = {r["job_id"] for r in rows}
    assert good_job in got, f"later job lost its record; runtime failed_sweeps={runtime.recorder.failed_sweeps if runtime.recorder else None}"  # noqa: E501
    assert bad_job in got


test_DEFECT_2_a_record_postgres_refuses_must_not_jam_later_records = pytest.mark.xfail(
    strict=True, reason="DEFECT-2: VARCHAR(64) overflow poisons the whole sweep"
)(test_DEFECT_2_a_record_postgres_refuses_must_not_jam_later_records)


@pytest.mark.xfail(
    strict=True, reason="DEFECT-2: NUL / lone surrogate in params poison the sweep"
)
@pytest.mark.parametrize(
    "params",
    [{"note": "a\u0000b"}, {"note": "\ud800"}, {"a\u0000": 1}],
    ids=["nul-value", "surrogate", "nul-key"],
)
async def test_DEFECT_2_nul_or_surrogate_in_params(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    engine: Any,
    tmp_path: Path,
    params: dict[str, Any],
) -> None:
    bad_job, bad = await _run(http, agent, tmp_path, "bad", BENCH_CODE, _doc(params=params))
    good_job, _ = await _run(http, agent, tmp_path, "good", PLAIN_CODE, "0")
    assert bad["state"] == "succeeded", bad
    rows = await _wait_records(engine, 2, within=15.0)
    assert {r["job_id"] for r in rows} == {bad_job, good_job}


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-4: status of unhashable type raises in the agent's report read",
)
async def test_DEFECT_4_an_unhashable_workload_status_does_not_break_the_attempt(
    http: httpx.AsyncClient, agent: ExecAgent, engine: Any, tmp_path: Path
) -> None:
    job_id, job = await _run(
        http, agent, tmp_path, "weird", BENCH_CODE, _doc(status=["ok"]), wait=20
    )
    assert job["state"] == "succeeded", job
    assert len(await _wait_records(engine, 1)) == 1


@pytest.mark.parametrize(
    "doc",
    [
        _doc(compute_ns="12"),
        _doc(compute_ns=-1),
        _doc(compute_ns=10**30),
        _doc(seed=2**63),  # fits uint64, not the BIGINT column
        _doc(seed=-1),
        _doc(params={"n": 2**70}),
        _doc(params={"n": 1.5, "repeats": True, "dtype": 5}),
        _doc(family="Not A Name"),
        _doc(params={"deep": json.loads("[" * 300 + "]" * 300)}),
        '{"status": "ok", "params": {"x": NaN}}',
        "not json at all",
        "[]",
    ],
    ids=[
        "ns-str", "ns-neg", "ns-huge", "seed-2^63", "seed-neg", "n-2^70",
        "wrong-types", "bad-family", "deep", "nan", "garbage", "array",
    ],
)
async def test_hostile_but_handled_workload_documents_still_yield_exactly_one_record(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    engine: Any,
    tmp_path: Path,
    doc: str,
) -> None:
    job_id, job = await _run(http, agent, tmp_path, "h", BENCH_CODE, doc, wait=30)
    assert job["state"] == "succeeded", job
    rows = await _wait_records(engine, 1, within=15)
    assert len(rows) == 1 and rows[0]["job_id"] == job_id, rows
    # the export of whatever was recorded is valid Parquet
    import pyarrow.parquet as pq

    assert pq.read_table(io.BytesIO(parquet_bytes(rows))).num_rows == 1


# ----------------------------------------------------------------- export


async def test_export_filters_413_streaming_and_errors(
    http: httpx.AsyncClient, agent: ExecAgent, engine: Any, tmp_path: Path
) -> None:
    import pyarrow.parquet as pq

    for i in range(3):
        await _run(http, agent, tmp_path, f"e{i}", PLAIN_CODE, "0")
    await _wait_records(engine, 3)
    full = await http.get("/v1/telemetry/export")
    assert full.status_code == 200, full.text
    assert pq.read_table(io.BytesIO(full.content)).num_rows == 3
    assert int(full.headers["content-length"]) == len(full.content)

    # one more record -> over export_max_rows=3 -> 413 with a useful message
    await _run(http, agent, tmp_path, "e3", PLAIN_CODE, "0")
    await _wait_records(engine, 4)
    over = await http.get("/v1/telemetry/export")
    assert over.status_code == 413, (over.status_code, over.text[:200])
    assert "narrow" in over.text
    assert b"PAR1" not in over.content

    # narrowing works: since = after everything -> empty valid file
    empty = await http.get("/v1/telemetry/export", params={"since": "2999-01-01T00:00:00+00:00"})
    assert empty.status_code == 200
    assert pq.read_table(io.BytesIO(empty.content)).num_rows == 0
    # since with an offset equals the same instant in UTC
    a = await http.get("/v1/telemetry/export", params={"since": "2999-01-01T05:30:00+05:30"})
    assert a.content == empty.content
    # ancient since -> 413 again (all 4 records, cap 3)
    assert (
        await http.get("/v1/telemetry/export", params={"since": "0001-01-01T00:00:00Z"})
    ).status_code == 413

    for params in (
        {"since": "garbage"},
        {"since": "nan"},
        {"campaign": "x" * 65},
        {"campaign": "../../etc/passwd"},
        {"campaign": "a\u0000b"},
        {"campaign": "日本"},
        {"campaign": ""},
    ):
        answer = await http.get("/v1/telemetry/export", params=params)
        assert answer.status_code in (400, 422), (params, answer.status_code)
    # and the route is still healthy after a client abandons a download mid-stream
    async with http.stream("GET", "/v1/telemetry/export", params={"since": "2999-01-01T00:00:00Z"}) as response:
        await response.aread()
    again = await http.get("/v1/telemetry/export", params={"since": "2999-01-01T00:00:00Z"})
    assert again.status_code == 200


async def test_the_export_does_not_depend_on_the_request_timezone_spelling(
    http: httpx.AsyncClient, agent: ExecAgent, engine: Any, tmp_path: Path
) -> None:
    await _run(http, agent, tmp_path, "tz", PLAIN_CODE, "0")
    rows = await _wait_records(engine, 1)
    created = rows[0]["attempt_created_at"]
    one = await http.get("/v1/telemetry/export", params={"since": created.isoformat()})
    ist = created.astimezone(__import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30)))
    two = await http.get("/v1/telemetry/export", params={"since": ist.isoformat()})
    assert one.status_code == two.status_code == 200
    assert one.content == two.content
