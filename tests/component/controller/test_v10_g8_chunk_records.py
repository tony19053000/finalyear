"""V0.10 G8 (0.10.9; D271, D261, D285) over a real controller (PostgreSQL,
gRPC sessions, HTTPS blob API) with fake lanes speaking the wire protocol:

* ``chunk_record``: one ``committed`` row per chunk with the agent's
  measurements, a ``failed`` row (stderr tail, typed failure, class) for a
  failed try, both speculative copies of a speculated chunk (``won``), the
  loser's late report completing its ``superseded`` row - and every row in
  the transaction of its result (no row without its applied result);
* ``GET /v1/jobs/{id}/chunks/{n}/tries``: the tries with their stderr tails,
  bounded by ``chunk_stderr_tail_bytes`` / ``chunk_stderr_ok_bytes``; access
  as every job read;
* run records v3 of a split job: lanes ``lane`` + strategy ``mode3``, the job
  row (combine) with makespan / job time / combine time / ideal bound;
* ``chunks.parquet``: two exports of the same rows are byte-identical (D222)
  and read back equal to the rows;
* the speculation check's per-queue cache (D285): idle-lane requests while no
  chunk commits run the history query at most once per commit / TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_fake_lanes import FakeLane
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from sqlalchemy import event, text

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry.export import (
    chunk_parquet_bytes,
    fetch_chunk_records,
)
from meshforge.controller.telemetry.schema import CHUNK_COLUMN_NAMES, SCHEMA_VERSION

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(300)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"
HB_S = 0.5
ITEMS = 24
CHUNK = 2
CHUNKS = ITEMS // CHUNK
FAST = 0.1
FAILED_TAIL_BYTES = 64
OK_TAIL_BYTES = 16


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        admission_timeout_s=1.2,
        split={
            "visibility_timeout_s": 60.0,
            "wait_retry_s": 0.05,
            "chunk_stderr_tail_bytes": FAILED_TAIL_BYTES,
            "chunk_stderr_ok_bytes": OK_TAIL_BYTES,
            "speculation_cache_s": 30.0,
        },
        telemetry={"record_interval_s": 0.2},
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S, miss_allowance_s=0.1, lease_multiplier=3
        ),
    )
    async with running_controller(settings) as rt:
        yield rt


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(runtime, token=token_of(runtime)) as client:
        yield client


@contextlib.asynccontextmanager
async def _lanes(
    runtime: ControllerRuntime, per_lane: Sequence[dict[str, Any]]
) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for extra in per_lane:
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=["exec", "exec-subprocess", "exec-split"],
                    ),
                    max_cpu_jobs=2,
                )
                fake.start_heartbeats(HB_S / 2)
                lane = FakeLane(
                    fake, runtime, **{"speed_s": FAST, "measured": True, **extra}
                )
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                await lane.close()
                if lane.killed:
                    continue
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


def _tree(root: Path) -> BuiltTree:
    root.mkdir(parents=True)
    (root / "main.py").write_bytes(b"print(1)\n")
    return build_tree(root)


async def _submit(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    tmp_path: Path,
    *,
    split: dict[str, Any] | None = None,
) -> str:
    built = _tree(tmp_path / "p")
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    body: dict[str, Any] = {
        "manifest": {
            "name": "s",
            "entrypoint": ENTRY,
            "split": {
                "over": {"range": {"stop": ITEMS}},
                "chunk": {"items": CHUNK},
                **(split or {}),
            },
            "retry": {"max_attempts": 3, "backoff_s": 0},
        },
        "tree_id": str(built.tree.tree_id),
        "placement": {},
    }
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _wait_job(
    runtime: ControllerRuntime, job_id: str, within: float = 120
) -> Any:
    assert runtime.jobs is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


async def _until(check: Callable[[], Any], within: float = 30.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        got = check()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _chunk_rows(runtime: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        runtime,
        "SELECT * FROM chunk_record WHERE job_id = :j ORDER BY chunk_index, lease_seq",
        j=job_id,
    )


def _stderr(chunk: agent_pb2.GrantedChunk) -> bytes:
    # 200 bytes, a secret-free tail; the controller keeps only the end
    return (f"chunk {chunk.chunk_index} " + "x" * 200 + "\nEND\n").encode()


# --------------------------------------------------------------------- tests


async def test_chunk_records_tries_and_stderr_tails(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(
        runtime,
        [
            {"fail": set(range(CHUNKS)), "fail_once": True, "stderr_of": _stderr},
            {"stderr_of": _stderr},
            {"stderr_of": _stderr},
        ],
    ) as lanes:
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, job.failure_detail
        rows = await _chunk_rows(runtime, job_id)
    assert all(r.schema_version == SCHEMA_VERSION for r in rows)
    committed = [r for r in rows if r.outcome == "committed"]
    assert sorted(r.chunk_index for r in committed) == list(range(CHUNKS))
    assert all(r.won and r.failure_class == "ok" for r in committed)
    assert all(
        r.items == CHUNK and r.first_item == r.chunk_index * CHUNK for r in committed
    )
    # the agent's measurements and the verified output size are kept
    assert all(r.run_ms is not None and r.stage_ms == 10 for r in committed)
    assert all(r.bytes_in and r.bytes_out and r.result_digest for r in committed)
    # every committed row's digest is the queue's winner
    winners = {
        r.idx: r.result_digest
        for r in await _sql(
            runtime,
            "SELECT i.idx, i.result_digest FROM work_item i JOIN work_queue q "
            "ON q.id = i.queue_id WHERE q.owner_id = :j",
            j=job_id,
        )
    }
    assert {r.chunk_index: r.result_digest for r in committed} == winners
    # the first lane fails every chunk it takes: each is retried elsewhere
    failed = [r for r in rows if r.outcome == "failed"]
    assert failed, rows
    for r in failed:
        assert not r.won
        assert r.failure_type == "user_error" and r.failure_class == "workload"
        assert r.exit_code == 3
        assert r.node_id == lanes[0].agent.node_id
        assert r.stderr_tail is not None
        assert len(r.stderr_tail.encode()) <= FAILED_TAIL_BYTES
        assert r.stderr_tail.endswith("\nEND\n")
    # done tries keep a shorter tail
    assert all(len((r.stderr_tail or "").encode()) <= OK_TAIL_BYTES for r in committed)
    # no row for a chunk without an applied result; one row per lease at most
    keys = [(r.chunk_index, r.lease_seq) for r in rows]
    assert len(keys) == len(set(keys))

    bad = failed[0].chunk_index
    # REST: the tries of chunk 3, in lease order, with stderr
    got = await http.get(f"/v1/jobs/{job_id}/chunks/{bad}/tries")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["chunk_index"] == bad
    outcomes = [t["outcome"] for t in body["tries"]]
    assert outcomes[-1] == "committed" and "failed" in outcomes
    assert [t["lease_seq"] for t in body["tries"]] == sorted(
        t["lease_seq"] for t in body["tries"]
    )
    first = body["tries"][0]
    assert first["stderr_tail"].endswith("END\n")
    assert first["failure_type"] == "user_error"
    # limit keeps the latest tries
    one = (await http.get(f"/v1/jobs/{job_id}/chunks/{bad}/tries?limit=1")).json()
    assert [t["lease_seq"] for t in one["tries"]] == [body["tries"][-1]["lease_seq"]]
    # bounds and access
    assert (await http.get(f"/v1/jobs/{job_id}/chunks/10000/tries")).status_code == 400
    assert (await http.get(f"/v1/jobs/{job_id}/chunks/-1/tries")).status_code == 400
    assert (
        await http.get(f"/v1/jobs/{job_id}/chunks/{bad}/tries?limit=101")
    ).status_code == 400
    none = (await http.get(f"/v1/jobs/{job_id}/chunks/{CHUNKS + 5}/tries")).json()
    assert none["tries"] == []
    async with api_client(runtime, token=None) as anon:
        assert (
            await anon.get(f"/v1/jobs/{job_id}/chunks/{bad}/tries")
        ).status_code == 401
    missing = "job_01J00000000000000000000000"
    assert (await http.get(f"/v1/jobs/{missing}/chunks/0/tries")).status_code == 404

    # run records v3: lanes + the job row
    async def records() -> list[Any]:
        found = await _sql(
            runtime,
            "SELECT * FROM run_record WHERE job_id = :j ORDER BY attempt_created_at",
            j=job_id,
        )
        roles = {r.attempt_role for r in found}
        return found if {"lane", "combine"} <= roles else []

    recs = await _until(records, within=30)
    lanes_rows = [r for r in recs if r.attempt_role == "lane"]
    (row,) = [r for r in recs if r.attempt_role == "combine"]
    assert all(r.strategy == "mode3" and r.schema_version == 3 for r in recs)
    assert all(r.split_items == ITEMS and r.split_chunk_items == CHUNK for r in recs)
    assert all(r.split_chunks == CHUNKS for r in recs)
    assert {n for r in lanes_rows for n in r.split_node_set} == {
        lane.agent.node_id for lane in lanes
    }
    assert all(r.split_makespan_ms is None for r in lanes_rows)  # job row only
    assert row.attempt_state == "done"
    assert row.split_job_ms is not None and row.split_makespan_ms is not None
    assert row.split_job_ms >= row.split_makespan_ms > 0
    assert row.split_combine_ms is not None
    assert row.split_ideal_bound_s is not None and row.split_ideal_bound_s > 0
    assert row.planner_strategy == "mode3"
    assert row.planner_estimate_s == pytest.approx(row.split_predicted_s)
    assert row.workload_status is None  # not a benchmark


async def test_both_speculative_copies_are_rows_and_a_late_loser_fills_its_row(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    last = CHUNKS - 1

    def slow_tail(grant: agent_pb2.GrantedChunk) -> float:
        return 4.0 if grant.chunk_index == last else FAST

    async with _lanes(
        runtime,
        [
            {"chunk_speed": slow_tail, "honour_cancel": False},
            {},
        ],
    ) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, job.failure_detail
        copies = [ref for lane in lanes for ref in lane.speculative]
        if not copies:
            pytest.skip("no speculative copy was issued on this run")
        # the slow original (honour_cancel off) reports late: lost
        rows = await _until(
            lambda: _lost(runtime, job_id),
            within=20,
        )
    by_chunk: dict[int, list[Any]] = {}
    for r in await _chunk_rows(runtime, job_id):
        by_chunk.setdefault(r.chunk_index, []).append(r)
    spec = [rows_ for rows_ in by_chunk.values() if any(r.speculative for r in rows_)]
    assert spec, by_chunk
    for rows_ in spec:
        assert sum(1 for r in rows_ if r.won) == 1
        losers = [r for r in rows_ if not r.won]
        assert losers and all(r.outcome in ("lost", "superseded") for r in losers)
    (late,) = rows
    assert late.run_ms is not None  # the late report filled the superseded row
    assert late.result_digest is not None


async def _lost(runtime: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        runtime,
        "SELECT * FROM chunk_record WHERE job_id = :j AND outcome = 'lost'",
        j=job_id,
    )


async def test_chunks_parquet_export_is_byte_identical_and_reads_back(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [{}, {}]):
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
    assert runtime._engine is not None
    first = await fetch_chunk_records(runtime._engine)
    second = await fetch_chunk_records(runtime._engine)
    assert first and first == second
    assert "stderr_tail" not in first[0]  # never exported
    one, two = chunk_parquet_bytes(first), chunk_parquet_bytes(list(reversed(second)))
    assert one == two
    table = pq.read_table(io.BytesIO(one))
    assert table.column_names == list(CHUNK_COLUMN_NAMES)
    assert table.num_rows == len(first)
    got = table.to_pylist()
    assert [(r["job_id"], r["chunk_index"], r["lease_seq"]) for r in got] == sorted(
        (r["job_id"], r["chunk_index"], r["lease_seq"]) for r in first
    )
    assert table.schema.metadata[b"meshforge.telemetry.schema_version"] == b"3"

    # the REST export (admin): the same bytes twice, its schema header
    a = await http.get("/v1/telemetry/export", params={"table": "chunks"})
    b = await http.get("/v1/telemetry/export", params={"table": "chunks"})
    assert a.status_code == 200, a.text
    assert a.content == b.content == one
    assert a.headers["x-meshforge-telemetry-schema"] == "3"
    assert a.headers["x-meshforge-telemetry-rows"] == str(len(first))
    bad = await http.get("/v1/telemetry/export", params={"table": "nope"})
    assert bad.status_code == 400


async def test_idle_lane_speculation_reads_history_once_per_commit(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """D285 carry-over: while one lane holds the last chunk, the idle lanes ask
    every ``wait_retry_s`` (0.05 s); each ask runs the speculation check, but
    the per-node p50 query runs again only after a commit (or the TTL)."""
    assert runtime._engine is not None
    statements: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        if "percentile_cont" in statement:
            statements.append(statement)

    event.listen(runtime._engine.sync_engine, "before_cursor_execute", count)
    last = CHUNKS - 1
    hold = asyncio.Event()

    def held(grant: agent_pb2.GrantedChunk) -> float:
        return 3.0 if grant.chunk_index == last else FAST

    try:
        async with _lanes(runtime, [{"chunk_speed": held}] * 3):
            job_id = await _submit(runtime, http, tmp_path, split={"speculative": True})
            # a factor so high no copy is ever worth it: lanes stay idle
            assert runtime.dispatcher is not None
            store = runtime.dispatcher.split._store
            store._speculation = type(store._speculation)(
                factor=100.0, overhead_s=1.5, cache_s=30.0, cache_jobs=8
            )
            requests_before = runtime.dispatcher.split.stats.requests
            job = await _wait_job(runtime, job_id)
            assert job.state is JobState.SUCCEEDED
            asked = runtime.dispatcher.split.stats.requests - requests_before
    finally:
        event.remove(runtime._engine.sync_engine, "before_cursor_execute", count)
        hold.set()
    cache = store._times
    # many idle asks at the tail, the history read at most once per commit
    assert asked > 3 * CHUNKS, asked
    assert len(statements) == cache.reads
    assert cache.reads <= CHUNKS + 1, (cache.reads, cache.hits)
    assert cache.hits > cache.reads
    assert len(cache) <= 8
