"""V1.0 G9-D (D437 r1): a chunk that ran past the job's time limit
(``deadline_exceeded``) over a real controller (PostgreSQL, gRPC sessions,
HTTPS blob API) with fake lanes:

* one lane (a pinned / "don't split" run) -> the job fails at once, one try;
* two lanes -> the chunk is retried once on the OTHER node, never on the node
  where it timed out, and the job succeeds;
* a live speculative copy on another node survives the original's time-limit
  failure (kept, not released) and wins.
"""

from __future__ import annotations

import asyncio
import contextlib
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

from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"
HB_S = 0.5  # lease 1.5 s: survives GC pauses of a one-process V0.10 run
ITEMS = 40
CHUNK = 2
CHUNKS = ITEMS // CHUNK
FAST = 0.2
SLOW = 5.0


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        admission_timeout_s=1.2,
        # leases outlive the slowest chunk (fake lanes never renew)
        split={
            "visibility_timeout_s": 60.0,
            "wait_retry_s": 0.1,
            # V1.0 G9-E (D438): these runs need lanes on a first run (no
            # history); with assumed rates the job would run whole on one lane
            "require_measured_rates": False,
        },
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
                lane = FakeLane(fake, runtime, **{"speed_s": FAST, **extra})
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
    items: int = ITEMS,
    chunk: int = CHUNK,
    split: dict[str, Any] | None = None,
    retry: dict[str, Any] | None = None,
    **body_extra: Any,
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
                "over": {"range": {"stop": items}},
                "chunk": {"items": chunk},
                **(split or {}),
            },
            **({"retry": retry} if retry else {}),
        },
        "tree_id": str(built.tree.tree_id),
        "placement": {},
        **body_extra,
    }
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _wait_job(
    runtime: ControllerRuntime, job_id: str, within: float = 120.0
) -> tuple[Any, float]:
    """The ended job and the loop time it was first seen ended."""
    assert runtime.jobs is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job, loop.time()
        await asyncio.sleep(0.02)
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
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _leases(runtime: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        runtime,
        "SELECT i.idx, l.seq, l.site, l.holder, l.state, l.speculative, "
        " l.leased_at, l.ended_at "
        "FROM work_lease l JOIN work_item i ON i.id = l.item_id "
        "JOIN work_queue q ON q.id = i.queue_id WHERE q.owner_id = :job "
        "ORDER BY i.idx, l.seq",
        job=job_id,
    )


async def _ledger_check(runtime: ControllerRuntime, job_id: str, chunks: int) -> None:
    """Exactly-once commit: every chunk done, exactly one completed lease each,
    no live lease left; a speculative copy never on its original's node."""
    rows = await _sql(
        runtime,
        "SELECT i.idx, i.state, "
        " (SELECT count(*) FROM work_lease l WHERE l.item_id = i.id "
        "  AND l.state = 'completed') AS completed, "
        " (SELECT count(*) FROM work_lease l WHERE l.item_id = i.id "
        "  AND l.state = 'live') AS live "
        "FROM work_item i JOIN work_queue q ON q.id = i.queue_id "
        "WHERE q.owner_id = :job ORDER BY i.idx",
        job=job_id,
    )
    assert [r.idx for r in rows] == list(range(chunks))
    assert all(r.state == "done" and r.completed == 1 for r in rows), rows
    assert all(r.live == 0 for r in rows)
    await _never_same_node(runtime, job_id)


async def _never_same_node(runtime: ControllerRuntime, job_id: str) -> None:
    by_idx: dict[int, list[Any]] = {}
    for row in await _leases(runtime, job_id):
        by_idx.setdefault(row.idx, []).append(row)
    for rows in by_idx.values():
        for copy in (r for r in rows if r.speculative):
            overlapping = [
                r
                for r in rows
                if r is not copy
                and r.leased_at <= copy.leased_at
                and (r.ended_at is None or r.ended_at > copy.leased_at)
            ]
            assert overlapping, "a copy is only issued while an original is live"
            assert all(r.site != copy.site for r in overlapping), rows


def _slow_from(first_slow: int, seconds: float = SLOW) -> Callable[[Any], float]:
    """A lane that slows down at the tail: chunks from ``first_slow`` on."""
    return lambda grant: seconds if grant.chunk_index >= first_slow else FAST


DEADLINE: dict[str, Any] = {
    "fail_kind": FailureType.DEADLINE_EXCEEDED,
    "fail_retryable": False,  # what the agent reports (D437)
}


async def _chunk_leases(runtime: ControllerRuntime, job_id: str, idx: int) -> list[Any]:
    return [r for r in await _leases(runtime, job_id) if r.idx == idx]


async def test_one_lane_fails_once_and_never_reruns_the_chunk(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [{"fail": {3}, **DEADLINE}]):
        job_id = await _submit(runtime, http, tmp_path, retry={"max_attempts": 10})
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.FAILED
        assert job.failure_type is not None
        assert job.failure_type.value == "deadline_exceeded"
        assert [r.state for r in await _chunk_leases(runtime, job_id, 3)] == ["failed"]


async def test_two_lanes_retry_it_once_on_the_other_node_never_the_same(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """4 chunks: the slow lane is busy with one chunk for 3 s while the fast
    lane runs the rest and times out on chunk 3. Chunk 3 is then the only
    pending one and the fast lane keeps asking - it must wait; the slow lane
    takes it when free and the job succeeds. (Re-run on the fast lane, it
    would time out again and fail the job.)"""
    async with _lanes(
        runtime,
        [{"speed_s": 0.05, "fail": {3}, **DEADLINE}, {"speed_s": 3.0}],
    ) as lanes:
        fast, slow = lanes
        job_id = await _submit(
            runtime, http, tmp_path, items=8, chunk=2, retry={"max_attempts": 10}
        )
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        rows = await _chunk_leases(runtime, job_id, 3)
        assert [r.state for r in rows] == ["failed", "completed"], rows
        assert rows[0].site == fast.agent.node_id
        assert rows[1].site == slow.agent.node_id
        assert [c for c, _ in fast.ledger].count(3) == 1  # never re-run there
        await _ledger_check(runtime, job_id, 4)


async def test_a_speculative_copy_survives_the_originals_time_limit(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    tail = set(range(CHUNKS - 4, CHUNKS))

    def copy_slow(grant: Any) -> float:
        return SLOW + 3.0 if grant.speculative else FAST

    async with _lanes(
        runtime,
        [
            {"chunk_speed": _slow_from(CHUNKS - 4), "fail": tail, **DEADLINE},
            {"chunk_speed": copy_slow},
        ],
    ) as lanes:
        slow, fast = lanes
        job_id = await _submit(runtime, http, tmp_path, retry={"max_attempts": 10})
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        assert len(fast.speculative) == 1
        straggler = fast.speculative[0][0]
        rows = await _chunk_leases(runtime, job_id, straggler)
        assert sorted((r.state, r.speculative) for r in rows) == [
            ("completed", True),
            ("failed", False),
        ], rows
        assert straggler in {c for c, _ in slow.ledger}  # the original ran there
        await _ledger_check(runtime, job_id, CHUNKS)
