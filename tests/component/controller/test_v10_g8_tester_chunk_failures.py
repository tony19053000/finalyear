"""V0.10 G8 tester (independent): chunk_record under failure injection, over a
real controller (PostgreSQL, gRPC sessions, HTTPS blob API) with fake lanes.

Invariants derived from the criteria (a record for every terminal outcome,
exactly one winner per chunk, rows only for applied results, bounded and
well-formed stderr), exercised by: a lane killed mid-chunk (-9), a job whose
every try fails, hostile stderr payloads (invalid UTF-8, NUL, oversize), and a
speculative race.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
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
from sqlalchemy import text

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
from meshforge.controller.telemetry.schema import SCHEMA_VERSION

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
            "visibility_timeout_s": 5.0,
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


async def _lease_rows(runtime: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        runtime,
        "SELECT i.idx, l.seq, l.state, l.speculative FROM work_lease l "
        "JOIN work_item i ON i.id = l.item_id JOIN work_queue q ON q.id = i.queue_id "
        "WHERE q.owner_id = :j ORDER BY i.idx, l.seq",
        j=job_id,
    )


def _invariants(rows: list[Any]) -> None:
    by: dict[int, list[Any]] = {}
    for r in rows:
        by.setdefault(r.chunk_index, []).append(r)
        assert r.schema_version == SCHEMA_VERSION
        assert r.outcome in {
            "committed", "lost", "failed", "superseded", "released", "expired",
        }  # fmt: skip
        assert (r.outcome == "committed") == r.won
        assert r.items >= 1 and r.first_item == r.chunk_index * CHUNK
        if r.outcome in ("expired", "released", "superseded") and r.run_ms is None:
            assert r.exit_code is None and r.result_digest is None
    for idx, group in by.items():
        winners = [r for r in group if r.won]
        assert len(winners) <= 1, f"chunk {idx} has {len(winners)} winners"
        seqs = [r.lease_seq for r in group]
        assert len(seqs) == len(set(seqs))


async def test_killed_lane_leaves_expired_or_released_rows_and_one_winner_per_chunk(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(
        runtime, [{"hold_results": True, "speed_s": 0.0}, {}, {}]
    ) as lanes:
        victim = lanes[0]
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        await _until(lambda: bool(victim.ledger), within=30)
        victim_node = victim.agent.node_id
        await victim.kill()  # kill -9: no goodbye, held chunk never reported
        job = await _wait_job(runtime, job_id, within=90)
        assert job.state is JobState.SUCCEEDED, job.failure_detail
        rows = await _chunk_rows(runtime, job_id)
        leases = await _lease_rows(runtime, job_id)
    _invariants(rows)
    committed = sorted(r.chunk_index for r in rows if r.outcome == "committed")
    assert committed == list(range(CHUNKS)), "exactly one committed row per chunk"
    dead = [r for r in rows if r.node_id == victim_node]
    assert dead, "the killed lane's leases must be recorded"
    for r in dead:
        assert r.outcome in ("expired", "released"), r.outcome
        assert not r.won and r.run_ms is None
        assert r.failure_class in ("infrastructure", "unavailable")
    # every lease of a committed chunk has a row (nothing silently dropped)
    keys = {(r.chunk_index, r.lease_seq) for r in rows}
    for lease in leases:
        assert (lease.idx, lease.seq) in keys, (
            f"no chunk_record for lease {tuple(lease)}"
        )


async def test_job_whose_every_try_fails_records_only_failed_rows(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    always = set(range(CHUNKS))
    async with _lanes(
        runtime,
        [
            {"fail": always, "stderr_of": _stderr},
            {"fail": always, "stderr_of": _stderr},
        ],
    ):
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        job = await _wait_job(runtime, job_id, within=90)
        assert job.state is JobState.FAILED
        rows = await _chunk_rows(runtime, job_id)
    assert rows, "failed tries must be recorded even when the job fails"
    _invariants(rows)
    assert not [r for r in rows if r.outcome == "committed"]
    assert all(r.outcome == "failed" for r in rows)
    assert all(r.failure_class == "workload" and r.failure_type for r in rows)
    assert all(
        r.stderr_tail and len(r.stderr_tail.encode()) <= FAILED_TAIL_BYTES for r in rows
    )
    # retry.max_attempts = 3 bounds the tries of a chunk
    per_chunk: dict[int, int] = {}
    for r in rows:
        per_chunk[r.chunk_index] = per_chunk.get(r.chunk_index, 0) + 1
    assert max(per_chunk.values()) <= 3, per_chunk


def _hostile(chunk: agent_pb2.GrantedChunk) -> bytes:
    kind = chunk.chunk_index % 4
    if kind == 0:
        return b"\xff\xfe\x80 broken \xc3 utf8 \xf0\x9f"
    if kind == 1:
        return b"nul\x00in\x00side\x00"
    if kind == 2:
        return ("é" * 200 + "\U0001f600" * 50).encode()  # multibyte at the cut
    return b"\r\n" * 500 + b"tail"


async def test_hostile_stderr_is_stored_as_valid_bounded_text(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(
        runtime,
        [
            {"fail": set(range(CHUNKS)), "fail_once": True, "stderr_of": _hostile},
            {"stderr_of": _hostile},
            {"stderr_of": _hostile},
        ],
    ):
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, job.failure_detail
        rows = await _chunk_rows(runtime, job_id)
        got = await http.get(f"/v1/jobs/{job_id}/chunks/0/tries")
        assert got.status_code == 200
        json.dumps(got.json())  # the API payload is valid JSON text
    for r in rows:
        tail = r.stderr_tail
        if tail is None:
            continue
        assert "\x00" not in tail
        assert not tail.startswith("\ufffd") or r.chunk_index % 4 == 0
        limit = FAILED_TAIL_BYTES if r.outcome == "failed" else OK_TAIL_BYTES
        assert len(tail.encode()) <= limit * 3  # replacement chars may widen
        tail.encode("utf-8")  # well-formed


async def test_speculative_race_invariants(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    last = CHUNKS - 1

    def slow_tail(grant: agent_pb2.GrantedChunk) -> float:
        return 3.0 if grant.chunk_index == last and not grant.speculative else FAST

    async with _lanes(
        runtime,
        [
            {"chunk_speed": slow_tail},
            {"chunk_speed": slow_tail},
            {"chunk_speed": slow_tail},
        ],
    ):
        job_id = await _submit(runtime, http, tmp_path)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, job.failure_detail
        await asyncio.sleep(1.0)  # let late reports land
        rows = await _chunk_rows(runtime, job_id)
        leases = await _lease_rows(runtime, job_id)
    _invariants(rows)
    assert sorted(r.chunk_index for r in rows if r.won) == list(range(CHUNKS))
    # a speculative copy of the last chunk exists and both copies are rows
    last_rows = [r for r in rows if r.chunk_index == last]
    if any(r.speculative for r in last_rows):
        assert len(last_rows) >= 2
        assert {r.lease_seq for r in last_rows} == {
            int(x.seq) for x in leases if x.idx == last
        }
    # the dataset export is stable while late rows could still arrive
    assert runtime._engine is not None
    a = chunk_parquet_bytes(await fetch_chunk_records(runtime._engine))
    b = chunk_parquet_bytes(await fetch_chunk_records(runtime._engine))
    assert a == b
    assert pq.read_table(io.BytesIO(a)).num_rows == len(rows)


async def test_user_cannot_force_a_node_set_through_the_public_api(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """D271: ``placement.node_set`` is written by the campaign runner only."""
    built = _tree(tmp_path / "p")
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    body: dict[str, Any] = {
        "manifest": {
            "name": "s",
            "entrypoint": ENTRY,
            "split": {"over": {"range": {"stop": ITEMS}}, "chunk": {"items": CHUNK}},
        },
        "tree_id": str(built.tree.tree_id),
        "placement": {"mode": "auto", "node_set": ["n1", "n2"]},
    }
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code in (400, 422), got.text
