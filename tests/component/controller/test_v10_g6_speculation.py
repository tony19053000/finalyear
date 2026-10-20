"""V0.10 G6 (0.10.5; D266, D269, D277): speculative copies of straggling chunks
over a real controller (PostgreSQL, gRPC sessions, HTTPS blob API) with fake
agents speaking the lane wire protocol (``mf_fake_lanes``).

* a lane that slows down 25x at the tail -> an idle lane gets a copy of its
  chunk, the copy wins, the slow original gets ``CancelChunk``, and the job
  ends about when the copy ends (not when the original would have);
* caps: ``ceil(speculative_max_frac x chunks)`` chunks ever get a copy; never
  on the node that holds the original; one node alone never copies;
  ``split.speculative: false`` never copies;
* the loser's late result -> ``already_done``, never a second commit; with
  another digest -> the job's ``nondeterministic_chunk`` warning, the winner
  kept;
* races: the copy's node LOST mid-copy -> the original finishes the job; the
  original's node LOST mid-copy -> the copy finishes it; a job cancel during
  speculation -> CANCELLED, no live lease left.
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
from mf_fake_lanes import FakeLane, default_output
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

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
        split={"visibility_timeout_s": 60.0, "wait_retry_s": 0.1},
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


def _expected_output(items: int = ITEMS, chunk: int = CHUNK) -> bytes:
    return b"".join(
        default_output(list(range(first, min(first + chunk, items))))
        for first in range(0, items, chunk)
    )


async def _blob(http: httpx.AsyncClient, digest: str) -> bytes:
    got = await http.get(f"/v1/blobs/{digest.removeprefix('sha256:')}")
    assert got.status_code == 200, got.text
    return got.content


def _slow_from(first_slow: int, seconds: float = SLOW) -> Callable[[Any], float]:
    """A lane that slows down at the tail: chunks from ``first_slow`` on."""
    return lambda grant: seconds if grant.chunk_index >= first_slow else FAST


def _stats(runtime: ControllerRuntime) -> Any:
    assert runtime.dispatcher is not None
    return runtime.dispatcher.split.stats


async def _queue_row(runtime: ControllerRuntime, job_id: str) -> Any:
    rows = await _sql(
        runtime,
        "SELECT state, item_count, speculated_count, speculative_max_frac "
        "FROM work_queue WHERE owner_id = :job",
        job=job_id,
    )
    return rows[0]


# ------------------------------------------------------------------ the tail


async def test_a_lane_slowed_at_the_tail_is_overtaken_by_a_copy(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [{"chunk_speed": _slow_from(CHUNKS - 4)}, {}]) as lanes:
        slow, fast = lanes
        job_id = await _submit(runtime, http, tmp_path)
        job, ended_at = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, CHUNKS)
        # the slow lane got exactly one tail chunk; a copy of it won
        starts = {(c, s): t for ev, t, c, s in slow.timeline if ev == "start"}
        slow_chunks = [k for k in starts if k[0] >= CHUNKS - 4]
        assert len(slow_chunks) == 1, slow.timeline
        straggler = slow_chunks[0]
        await _until(lambda: slow.cancelled)  # the fake reads its inbox every 10 ms
        assert slow.cancelled == [straggler]  # CancelChunk stopped it
        assert [c for c, _ in fast.speculative] == [straggler[0]]
        copy = fast.speculative[0]
        assert copy in fast.ledger
        stats = _stats(runtime)
        assert stats.speculated == 1 and stats.copies_won == 1
        assert stats.originals_won == 0
        # the job ends about when the copy ends, far before the original
        copy_done = next(
            t for ev, t, c, s in fast.timeline if (c, s) == copy and ev == "result"
        )
        slow_start = starts[straggler]
        assert ended_at < slow_start + SLOW - 1.5, ended_at - slow_start
        assert ended_at - copy_done < 2.0, ended_at - copy_done
        # every chunk's result is the honest one
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert detail["warnings"] == []
        assert await _blob(http, detail["output_digest"]) == _expected_output()
        queue = await _queue_row(runtime, job_id)
        assert queue.speculated_count == 1 and queue.speculative_max_frac == 0.10


async def test_budget_counts_chunks_and_holds(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """10 chunks x 0.10 -> one chunk may ever get a copy: two lanes straggle
    on their first chunk, the fast third lane copies only one of them."""
    async with _lanes(
        runtime, [{"speed_s": 3.0}, {"speed_s": 3.0}, {"speed_s": 0.05}]
    ) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=20, chunk=2)
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 10)
        assert len(lanes[2].speculative) == 1
        assert not lanes[0].speculative and not lanes[1].speculative
        queue = await _queue_row(runtime, job_id)
        assert queue.speculated_count == 1
        copies = [r for r in await _leases(runtime, job_id) if r.speculative]
        assert len(copies) == 1
        assert _stats(runtime).speculated == 1


async def test_one_node_never_copies_its_own_chunk(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [{"chunk_speed": _slow_from(8, 1.5)}]):
        job_id = await _submit(runtime, http, tmp_path, items=20, chunk=2)
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        assert not [r for r in await _leases(runtime, job_id) if r.speculative]
        assert _stats(runtime).speculated == 0


async def test_speculative_false_never_copies(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(
        runtime, [{"chunk_speed": _slow_from(CHUNKS - 4, 2.0)}, {}]
    ) as lanes:
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        assert not lanes[1].speculative and not lanes[0].cancelled
        queue = await _queue_row(runtime, job_id)
        assert queue.speculative_max_frac == 0.0 and queue.speculated_count == 0


# ------------------------------------------------------------------ the loser


async def test_the_losers_late_result_is_already_done_never_a_second_commit(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    slow = {"chunk_speed": _slow_from(CHUNKS - 4, 2.5), "honour_cancel": False}
    async with _lanes(runtime, [slow, {}]) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        # the loser still finishes and reports: wait for its lane to end
        await _until(lambda: lanes[0].stopped)
        stats = _stats(runtime)
        await _until(lambda: stats.already_done >= 1)
        assert stats.copies_won == 1 and stats.nondeterministic == 0
        await _ledger_check(runtime, job_id, CHUNKS)
        superseded = [
            r
            for r in await _leases(runtime, job_id)
            if not r.speculative and r.state == "superseded"
        ]
        assert len(superseded) == 1
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert detail["warnings"] == []


def _other_bytes(items: Sequence[str | int]) -> bytes:
    return b"X" + default_output(items)


async def test_a_loser_with_other_bytes_is_a_warning_and_the_winner_is_kept(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    slow = {
        "chunk_speed": _slow_from(CHUNKS - 4, 2.5),
        "honour_cancel": False,
        "output_of": lambda items: (
            _other_bytes(items)
            if int(items[0]) >= (CHUNKS - 4) * CHUNK
            else default_output(items)
        ),
    }
    async with _lanes(runtime, [slow, {}]) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        copy_chunk = lanes[1].speculative[0][0]

        async def warned() -> Any:
            detail = (await http.get(f"/v1/jobs/{job_id}")).json()
            return detail if detail["warnings"] else None

        detail = await _until(warned)
        assert detail["warnings"][0]["code"] == "nondeterministic_chunk"
        assert detail["warnings"][0]["chunks"] == [copy_chunk]
        assert _stats(runtime).nondeterministic == 1
        assert await _blob(http, detail["output_digest"]) == _expected_output()
        await _ledger_check(runtime, job_id, CHUNKS)


# ------------------------------------------------------------------ races


async def test_copy_node_lost_mid_copy_the_original_finishes(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    def copy_is_slow(grant: Any) -> float:
        return 30.0 if grant.speculative else FAST

    async with _lanes(
        runtime,
        [{"chunk_speed": _slow_from(CHUNKS - 4, 3.0)}, {"chunk_speed": copy_is_slow}],
    ) as lanes:
        original, copier = lanes
        job_id = await _submit(runtime, http, tmp_path)
        await _until(lambda: copier.speculative)
        await copier.kill()
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, CHUNKS)
        rows = await _leases(runtime, job_id)
        copies = [r for r in rows if r.speculative]
        assert [r.state for r in copies] == ["released"]  # with the LOST lane
        assert not original.cancelled
        lost = await _sql(
            runtime,
            "SELECT state FROM attempt WHERE job_id = :job AND node_id = :node",
            job=job_id,
            node=copier.agent.node_id,
        )
        assert [r.state for r in lost] == ["lost"]
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert await _blob(http, detail["output_digest"]) == _expected_output()


async def test_original_node_lost_mid_copy_the_copy_finishes(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    def copy_takes_a_while(grant: Any) -> float:
        return 1.5 if grant.speculative else FAST

    async with _lanes(
        runtime,
        [
            {"chunk_speed": _slow_from(CHUNKS - 4, 30.0)},
            {"chunk_speed": copy_takes_a_while},
        ],
    ) as lanes:
        original, copier = lanes
        job_id = await _submit(runtime, http, tmp_path)
        await _until(lambda: copier.speculative)
        await original.kill()
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, CHUNKS)
        rows = await _leases(runtime, job_id)
        copy = next(r for r in rows if r.speculative)
        assert copy.state == "completed"
        # the original's lease went back with its lane (released, not expired)
        assert any(
            r.idx == copy.idx and not r.speculative and r.state == "released"
            for r in rows
        ), rows
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert await _blob(http, detail["output_digest"]) == _expected_output()


async def test_a_cancel_during_speculation_ends_cancelled_with_no_live_lease(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    def copy_is_slow(grant: Any) -> float:
        return 30.0 if grant.speculative else FAST

    async with _lanes(
        runtime,
        [
            {"chunk_speed": _slow_from(CHUNKS - 4, 30.0)},
            {"chunk_speed": copy_is_slow},
        ],
    ) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        await _until(lambda: lanes[1].speculative)
        got = await http.post(f"/v1/jobs/{job_id}/cancel")
        assert got.status_code in (200, 202), got.text
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.CANCELLED
        await _until(lambda: all(lane.stopped for lane in lanes))
        rows = await _leases(runtime, job_id)
        assert not [r for r in rows if r.state == "live"], rows
        queue = await _queue_row(runtime, job_id)
        assert queue.state == "cancelled"
        roles = await _sql(
            runtime, "SELECT role FROM attempt WHERE job_id = :job", job=job_id
        )
        assert "combine" not in {r.role for r in roles}  # no partial output
