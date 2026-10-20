"""V0.10 G4 (0.10.4 / 0.10.6; D259, D261, D263, D267): a split job's lanes over
a real controller (PostgreSQL, gRPC sessions, HTTPS blob API) with fake agents
speaking the lane protocol (``mf_fake_lanes``).

* 3 lanes x 300 items, uneven speeds -> SUCCEEDED, every chunk committed once
  (one completed lease per chunk), the fast lane ran more chunks, the parts
  index lists every chunk's digest in chunk order;
* a lane's node killed mid-run (stream dropped, no Goodbye) -> OFFLINE -> the
  lane LOST and its leases released in that transaction; the others finish the
  job with the SAME parts index as an undisturbed run;
* a chunk failing on two nodes -> the job FAILED (user_error, the chunk named),
  no parts index, the other lanes cancelled;
* fencing: a chunk message with a stale token or from another node is counted
  and never applied;
* expiry + late result: a lease that expired goes back to pending, another lane
  commits it, the late result is ``already_done`` - one commit;
* the last lane lost with chunks left -> the job is re-planned onto new lanes
  for the remaining chunks only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Sequence
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

from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.split_chunks import parts_index_blob
from meshforge.common.states import JobState
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"
HB_S = 0.2


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        split={"visibility_timeout_s": 5.0, "wait_retry_s": 0.2},
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
    runtime: ControllerRuntime, speeds: Sequence[float], **kw: Any
) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for speed in speeds:
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
                lane = FakeLane(fake, runtime, speed_s=speed, **kw)
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                await lane.close()
                if lane.killed:
                    continue  # its call is already cancelled
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


def _tree(root: Path) -> BuiltTree:
    root.mkdir(parents=True)
    (root / "main.py").write_bytes(b"print(1)\n")
    return build_tree(root)


def _store(runtime: ControllerRuntime, built: BuiltTree) -> None:
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)


async def _submit(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    tmp_path: Path,
    *,
    items: int = 300,
    chunk: int = 10,
    name: str = "p",
    **manifest: Any,
) -> str:
    built = _tree(tmp_path / name)
    _store(runtime, built)
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "s",
                "entrypoint": ENTRY,
                "split": {
                    "over": {"range": {"stop": items}},
                    "chunk": {"items": chunk},
                },
                **manifest,
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _wait_job(
    runtime: ControllerRuntime, job_id: str, within: float = 120.0
) -> Any:
    assert runtime.jobs is not None
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _ledger_check(runtime: ControllerRuntime, job_id: str, chunks: int) -> None:
    """Exactly-once commit: every chunk done, exactly one completed lease each,
    no live lease left."""
    rows = await _sql(
        runtime,
        "SELECT i.idx, i.state, i.result_digest, "
        " (SELECT count(*) FROM work_lease l WHERE l.item_id = i.id "
        "  AND l.state = 'completed') AS completed, "
        " (SELECT count(*) FROM work_lease l WHERE l.item_id = i.id "
        "  AND l.state = 'live') AS live "
        "FROM work_item i JOIN work_queue q ON q.id = i.queue_id "
        "WHERE q.owner_id = :job ORDER BY i.idx",
        job=job_id,
    )
    assert [r.idx for r in rows] == list(range(chunks))
    assert all(r.state == "done" for r in rows), rows
    assert all(r.completed == 1 for r in rows), [r for r in rows if r.completed != 1]
    assert all(r.live == 0 for r in rows)


def _expected_parts(items_digest: str, items: int, chunk: int) -> str:
    chunks = []
    for first in range(0, items, chunk):
        count = min(chunk, items - first)
        data = default_output(list(range(first, first + count)))
        chunks.append((first, count, str(sha256_bytes(data))))
    blob = parts_index_blob(items_digest=items_digest, chunk_items=chunk, chunks=chunks)
    return str(sha256_bytes(blob))


# ------------------------------------------------------------------ happy path


async def test_three_lanes_uneven_speeds_commit_every_chunk_once(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.005, 0.05, 0.05]) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 30)
        ran = [len(lane.ledger) for lane in lanes]
        assert sum(ran) == 30
        assert ran[0] > ran[1] and ran[0] > ran[2], ran  # pull = balancing
        assert job.split is not None and job.split.parts_digest is not None
        assert job.split.parts_digest == _expected_parts(
            job.split.items_digest, 300, 10
        )
        # the parts index is stored in the controller's CAS
        assert runtime.blobs is not None
        assert runtime.blobs.cas.has(Digest.parse(job.split.parts_digest))
        lane_rows = await _sql(
            runtime,
            "SELECT role, state FROM attempt WHERE job_id = :job",
            job=job_id,
        )
        # V0.10 G5: the three lanes, then the job's combine
        assert sorted(r.role for r in lane_rows) == ["combine", "lane", "lane", "lane"]
        # the lanes drained and ended DONE once the queue was done
        for _ in range(100):
            states = {
                r.state
                for r in await _sql(
                    runtime, "SELECT state FROM attempt WHERE job_id = :job", job=job_id
                )
            }
            if states == {"done"}:
                break
            await asyncio.sleep(0.05)
        assert states == {"done"}
        assert runtime.dispatcher is not None
        stats = runtime.dispatcher.split.stats
        assert stats.committed == 30 and stats.fenced == {}


# ------------------------------------------------------------------ failures


async def test_killed_lane_is_lost_and_its_leases_go_back_in_the_same_tx(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    # the victim's chunk takes 30 s: once it holds a live lease it is still
    # holding it when it is killed (fix r2 B2: no race with its completion)
    async with _lanes(runtime, [0.02, 0.02, 30.0]) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        victim = lanes[2]
        held: list[Any] = []
        for _ in range(800):
            held = await _sql(
                runtime,
                "SELECT l.id FROM work_lease l JOIN attempt a ON a.id = l.holder "
                "WHERE a.job_id = :job AND a.node_id = :node AND l.state = 'live'",
                job=job_id,
                node=victim.agent.node_id,
            )
            if held:
                break
            await asyncio.sleep(0.01)
        assert held, "the slow lane never held a chunk"
        await victim.kill()
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 30)
        lost = await _sql(
            runtime,
            "SELECT id, state, failure_type, detail FROM attempt "
            "WHERE job_id = :job AND node_id = :node",
            job=job_id,
            node=victim.agent.node_id,
        )
        assert [r.state for r in lost] == ["lost"], lost
        assert lost[0].failure_type == "node_lost"
        # its lease was released (not expired): the release happened with LOST
        leases = await _sql(
            runtime,
            "SELECT state FROM work_lease WHERE holder = :holder",
            holder=lost[0].id,
        )
        assert "released" in {r.state for r in leases}, leases
        assert "expired" not in {r.state for r in leases}, leases
        assert job.split is not None
        assert job.split.parts_digest == _expected_parts(
            job.split.items_digest, 300, 10
        )


async def test_chunk_failing_on_two_nodes_fails_the_job_with_no_output(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.01, 0.01, 0.01], fail={7}) as lanes:
        # many tries allowed: the job must fail by the two-node rule (D267c),
        # not because one lane retried the chunk max_attempts times
        job_id = await _submit(runtime, http, tmp_path, retry={"max_attempts": 10})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.FAILED
        assert job.failure_type is not None and job.failure_type.value == "user_error"
        assert job.failure_detail is not None
        assert job.failure_detail.startswith(
            "chunk_failed: chunk 7 failed on 2 node(s)"
        )
        assert job.split is not None and job.split.parts_digest is None
        queue = await _sql(
            runtime, "SELECT state FROM work_queue WHERE owner_id = :job", job=job_id
        )
        assert [r.state for r in queue] == ["failed"]
        # every lane stops (cancelled, or drained/stopped by NoWork)
        for _ in range(200):
            live = await _sql(
                runtime,
                "SELECT count(*) AS n FROM attempt WHERE job_id = :job "
                "AND state NOT IN ('done', 'failed', 'lost', 'fenced')",
                job=job_id,
            )
            if live[0].n == 0:
                break
            await asyncio.sleep(0.05)
        assert live[0].n == 0
        failed_on = await _sql(
            runtime,
            "SELECT i.failed_sites FROM work_item i JOIN work_queue q "
            "ON q.id = i.queue_id WHERE q.owner_id = :job AND i.idx = 7",
            job=job_id,
        )
        assert len(set(failed_on[0].failed_sites)) == 2
        assert {lane.agent.node_id for lane in lanes} >= set(failed_on[0].failed_sites)


async def test_last_lane_lost_replans_new_lanes_for_the_remaining_chunks(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.05, 0.05]) as first:
        job_id = await _submit(runtime, http, tmp_path, items=100, chunk=10)
        for _ in range(400):
            if sum(len(lane.ledger) for lane in first) >= 3:
                break
            await asyncio.sleep(0.01)
        for lane in first:
            await lane.kill()
        # both lanes LOST -> RETRYING -> QUEUED; nothing can run it until a
        # node comes back
        for _ in range(200):
            job = await runtime.jobs.get_job(job_id) if runtime.jobs else None
            if job is not None and job.state is JobState.QUEUED:
                break
            await asyncio.sleep(0.05)
        assert job is not None and job.state is JobState.QUEUED, job
        done_before = await _sql(
            runtime,
            "SELECT count(*) AS n FROM work_item i JOIN work_queue q "
            "ON q.id = i.queue_id WHERE q.owner_id = :job AND i.state = 'done'",
            job=job_id,
        )
    async with _lanes(runtime, [0.0]) as second:
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 10)
        # the new lane ran only what was left (committed chunks are kept)
        assert len(second[0].ledger) <= 10 - done_before[0].n
        queues = await _sql(
            runtime,
            "SELECT count(*) AS n FROM work_queue WHERE owner_id = :job",
            job=job_id,
        )
        assert queues[0].n == 1


# ------------------------------------------------------------------ by hand


async def test_fencing_expiry_and_a_late_result_commit_once(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """One lane driven by hand, then a second: a wrong token and a wrong node
    are fenced; an expired lease goes back to pending; the other lane commits
    it; the late result of the first is already_done."""
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        agents = []
        for _ in range(2):
            fake = FakeAgent(channel)
            await fake.connect(
                hardware=agent_pb2.HardwareSummary(
                    cpu_model="t",
                    cpu_cores=2,
                    cpu_threads=2,
                    ram_bytes=8 * GIB,
                    capabilities=["exec", "exec-subprocess", "exec-split"],
                )
            )
            fake.start_heartbeats(HB_S / 2)
            agents.append(fake)
        try:
            job_id = await _submit(
                runtime, http, tmp_path, items=4, chunk=1, retry={"max_attempts": 5}
            )

            async def start_of(fake: FakeAgent) -> agent_pb2.StartAttempt:
                for _ in range(400):
                    for msg in fake.received:
                        if msg.WhichOneof("payload") == "start_attempt":
                            return msg.start_attempt
                    await asyncio.sleep(0.01)
                raise AssertionError("no StartAttempt")

            a, b = agents
            sa, sb = await start_of(a), await start_of(b)

            async def send(fake: FakeAgent, **payload: Any) -> None:
                await fake.write(
                    agent_pb2.AgentMessage(
                        message_id=new_command_id(),
                        session_id=fake.session_id,
                        **payload,
                    )
                )

            async def reply(fake: FakeAgent, after: int) -> agent_pb2.ControllerMessage:
                for _ in range(400):
                    for msg in fake.received[after:]:
                        if msg.WhichOneof("payload") in ("chunk_grant", "no_work"):
                            return msg
                    await asyncio.sleep(0.01)
                raise AssertionError("no reply")

            def request(start: agent_pb2.StartAttempt, token: int | None = None) -> Any:
                return agent_pb2.ChunkRequest(
                    job_id=start.job_id,
                    attempt_id=start.attempt_id,
                    fencing_token=start.fencing_token if token is None else token,
                    want=1,
                )

            # a stale token and another node's lane are fenced, never applied
            assert runtime.dispatcher is not None
            stats = runtime.dispatcher.split.stats
            seen = len(a.received)
            await send(a, chunk_request=request(sa, token=sa.fencing_token + 50))
            await send(a, chunk_request=request(sb))  # b's lane from a's session
            stop = await reply(a, seen)
            assert stop.no_work.reason == agent_pb2.NoWork.REASON_STOP
            for _ in range(100):
                if sum(stats.fenced.values()) >= 2:
                    break
                await asyncio.sleep(0.02)
            assert stats.fenced.get("stale_token") == 1, stats.fenced
            assert stats.fenced.get("wrong_node") == 1, stats.fenced

            # a takes chunk 0 and goes quiet past the 5 s visibility timeout
            seen = len(a.received)
            await send(a, chunk_request=request(sa))
            grant = (await reply(a, seen)).chunk_grant.chunks[0]
            assert grant.chunk_index == 0
            for _ in range(200):
                rows = await _sql(
                    runtime,
                    "SELECT l.state FROM work_lease l WHERE l.holder = :h",
                    h=sa.attempt_id,
                )
                if [r.state for r in rows] == ["expired"]:
                    break
                await asyncio.sleep(0.05)
            assert [r.state for r in rows] == ["expired"]

            # b now gets chunk 0 again (pending after expiry) and commits it
            async with api_client(
                runtime, token=sb.spec.upload_token, node_id=b.node_id
            ) as up_b:
                seen = len(b.received)
                await send(b, chunk_request=request(sb))
                got = (await reply(b, seen)).chunk_grant.chunks[0]
                assert got.chunk_index == 0 and got.lease_seq == grant.lease_seq + 1
                data = default_output([0])
                digest = sha256_bytes(data)
                put = await up_b.put(f"/v1/blobs/{digest.hex}", content=data)
                assert put.status_code in (200, 201), put.text
                await send(
                    b,
                    chunk_result=agent_pb2.ChunkResult(
                        job_id=sb.job_id,
                        attempt_id=sb.attempt_id,
                        fencing_token=sb.fencing_token,
                        chunk_index=0,
                        item_id=got.item_id,
                        lease_seq=got.lease_seq,
                        outcome=agent_pb2.ChunkResult.OUTCOME_DONE,
                        result_digest=str(digest),
                    ),
                )
            # a's late result for the same chunk: verified, but already done
            async with api_client(
                runtime, token=sa.spec.upload_token, node_id=a.node_id
            ) as up_a:
                put = await up_a.put(f"/v1/blobs/{digest.hex}", content=data)
                assert put.status_code in (200, 201), put.text
            await send(
                a,
                chunk_result=agent_pb2.ChunkResult(
                    job_id=sa.job_id,
                    attempt_id=sa.attempt_id,
                    fencing_token=sa.fencing_token,
                    chunk_index=0,
                    item_id=grant.item_id,
                    lease_seq=grant.lease_seq,
                    outcome=agent_pb2.ChunkResult.OUTCOME_DONE,
                    result_digest=str(digest),
                ),
            )
            for _ in range(200):
                if stats.committed >= 1 and stats.already_done >= 1:
                    break
                await asyncio.sleep(0.02)
            assert stats.committed == 1 and stats.already_done == 1, stats
            rows = await _sql(
                runtime,
                "SELECT i.state, i.done_lease_seq FROM work_item i "
                "JOIN work_queue q ON q.id = i.queue_id "
                "WHERE q.owner_id = :job AND i.idx = 0",
                job=job_id,
            )
            assert rows[0].state == "done" and rows[0].done_lease_seq == got.lease_seq
        finally:
            for fake in agents:
                with contextlib.suppress(Exception):
                    await fake.close()


async def test_split_item_list_blob_is_readable_only_by_lanes(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The lane's fetch token covers the item list (LaneSpec.items_digest);
    the fake lane downloads and verifies it before pulling - this runs the
    whole job with one lane ("don't split": min_nodes 2 not met)."""
    async with _lanes(runtime, [0.0]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=20, chunk=5)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        assert len(lanes[0].ledger) == 4
        decision = await _sql(
            runtime,
            "SELECT outcome, chosen_node_id FROM plan_decision WHERE job_id = :job",
            job=job_id,
        )
        assert [r.outcome for r in decision] == ["dispatched"]
        assert decision[0].chosen_node_id == lanes[0].agent.node_id
        assert json.loads(json.dumps(job.split.spec))["chunk"] == {"items": 5}
