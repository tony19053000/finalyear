"""V0.10 G4 tester r1: lane isolation (cross-lane commit, blob scope) and a
bounded re-plan when lanes keep being lost."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_fabric import NodeChannels
from mf_fake_lanes import default_output
from mf_jobs_harness import api_client
from mf_session_client import FakeAgent
from test_v10_g4_lanes import (  # type: ignore[import-not-found]
    GIB,
    HB_S,
    _lanes,
    _sql,
    _submit,
    _wait_job,
    http,  # noqa: F401
    runtime,  # noqa: F401
)

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]


async def test_a_lane_cannot_commit_a_chunk_leased_to_another_lane_or_fetch_foreign_blobs(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        agents: list[FakeAgent] = []
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

            async def send(fake: FakeAgent, **payload: Any) -> None:
                await fake.write(
                    agent_pb2.AgentMessage(
                        message_id=new_command_id(),
                        session_id=fake.session_id,
                        **payload,
                    )
                )

            a, b = agents
            sa, sb = await start_of(a), await start_of(b)
            seen = len(a.received)
            await send(
                a,
                chunk_request=agent_pb2.ChunkRequest(
                    job_id=sa.job_id,
                    attempt_id=sa.attempt_id,
                    fencing_token=sa.fencing_token,
                    want=1,
                ),
            )
            grant = None
            for _ in range(400):
                for msg in a.received[seen:]:
                    if msg.WhichOneof("payload") == "chunk_grant":
                        grant = msg.chunk_grant.chunks[0]
                if grant:
                    break
                await asyncio.sleep(0.01)
            assert grant is not None
            data = default_output([grant.chunk_index])
            digest = sha256_bytes(data)
            # b uploads the correct bytes under ITS grant and commits a's chunk
            async with api_client(
                runtime, token=sb.spec.upload_token, node_id=b.node_id
            ) as up_b:
                assert (
                    await up_b.put(f"/v1/blobs/{digest.hex}", content=data)
                ).status_code in (200, 201)
            await send(
                b,
                chunk_result=agent_pb2.ChunkResult(
                    job_id=sb.job_id,
                    attempt_id=sb.attempt_id,
                    fencing_token=sb.fencing_token,
                    chunk_index=grant.chunk_index,
                    item_id=grant.item_id,
                    lease_seq=grant.lease_seq,
                    outcome=agent_pb2.ChunkResult.OUTCOME_DONE,
                    result_digest=str(digest),
                ),
            )
            await asyncio.sleep(1.0)
            rows = await _sql(
                runtime,
                "SELECT i.state FROM work_item i JOIN work_queue q ON q.id=i.queue_id "
                "WHERE q.owner_id=:j AND i.idx=:k",
                j=job_id,
                k=grant.chunk_index,
            )
            assert rows[0].state != "done", "lane b committed lane a's chunk"
            live = await _sql(
                runtime,
                "SELECT holder FROM work_lease WHERE item_id=:i AND state='live'",
                i=grant.item_id,
            )
            assert [r.holder for r in live] == [sa.attempt_id]
            # a claims b's upload as its own: not in a's grant -> not committed
            await send(
                a,
                chunk_result=agent_pb2.ChunkResult(
                    job_id=sa.job_id,
                    attempt_id=sa.attempt_id,
                    fencing_token=sa.fencing_token,
                    chunk_index=grant.chunk_index,
                    item_id=grant.item_id,
                    lease_seq=grant.lease_seq,
                    outcome=agent_pb2.ChunkResult.OUTCOME_DONE,
                    result_digest=str(digest),
                ),
            )
            await asyncio.sleep(1.0)
            rows = await _sql(
                runtime,
                "SELECT i.state FROM work_item i JOIN work_queue q ON q.id=i.queue_id "
                "WHERE q.owner_id=:j AND i.idx=:k",
                j=job_id,
                k=grant.chunk_index,
            )
            assert rows[0].state != "done", "a committed bytes it never uploaded"

            # blob scope: a random blob stored by someone else is not fetchable
            assert runtime.blobs is not None
            secret = b"not-for-lanes" * 100
            sd = sha256_bytes(secret)
            runtime.blobs.cas.put_bytes(secret, sd)
            async with api_client(runtime, node_id=a.node_id) as cli:
                got = await cli.get(
                    f"/v1/blobs/{sd.hex}",
                    headers={"authorization": f"Bearer {sa.spec.fetch_token}"},
                )
                assert got.status_code in (401, 403, 404), got.status_code
                # an upload token is not a read credential, and a lane's upload
                # token cannot be used by the other node
                got = await cli.get(
                    f"/v1/blobs/{digest.hex}",
                    headers={"authorization": f"Bearer {sb.spec.fetch_token}"},
                )
                assert got.status_code in (401, 403, 404), got.status_code
        finally:
            for fake in agents:
                with contextlib.suppress(Exception):
                    await fake.close()


async def test_replan_after_losing_every_lane_is_bounded_by_max_attempts(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    job_id = ""
    for _round in range(2):
        async with _lanes(runtime, [0.2, 0.2]) as lanes:
            if not job_id:
                job_id = await _submit(
                    runtime,
                    http,
                    tmp_path,
                    items=100,
                    chunk=10,
                    retry={"max_attempts": 2},
                )
            for _ in range(300):
                if all(lane.ledger for lane in lanes):
                    break
                job = await runtime.jobs.get_job(job_id)  # type: ignore[union-attr]
                if job is not None and job.state is JobState.FAILED:
                    break
                await asyncio.sleep(0.02)
            for lane in lanes:
                await lane.kill()
        # wait until the job is re-queued or ended
        for _ in range(200):
            job = await runtime.jobs.get_job(job_id)  # type: ignore[union-attr]
            if job is not None and job.state in (JobState.QUEUED, JobState.FAILED):
                break
            await asyncio.sleep(0.05)
        if job is not None and job.state is JobState.FAILED:
            break
    job = await _wait_job(runtime, job_id, within=60)
    assert job.state is JobState.FAILED, (job.state, job.failure_type)
    rounds = await _sql(
        runtime, "SELECT count(*) AS n FROM attempt WHERE job_id=:j", j=job_id
    )
    assert rounds[0].n <= 4, f"{rounds[0].n} lane attempts for max_attempts=2"
    queue = await _sql(
        runtime, "SELECT state FROM work_queue WHERE owner_id=:j", j=job_id
    )
    assert queue[0].state in ("cancelled", "failed")
