"""V0.10 G4 fix round 1: the gaps tester r1 could not cover.

* a ``ChunkResult`` with a stale lane token is fenced (counted, never applied) -
  not only a ``ChunkRequest``;
* lane attempts never feed the planner's whole-run history (``_history`` /
  ``_output_history`` - the source of ``History.by_node_s``), although they do
  record run phases;
* two live lanes of one job on one node: the planner never produces it (one
  ``LanePlan`` per node, D267f) and the database refuses it anyway
  (``ux_attempt_live_lane``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_fabric import NodeChannels
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
from meshforge.common.ids import new_attempt_id, new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.controller.jobs.queue_reads import _history, _output_history
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_session_factory

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

CAPS = ["exec", "exec-subprocess", "exec-split"]


async def test_a_chunk_result_with_a_stale_token_is_fenced(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        fake = FakeAgent(channel)
        await fake.connect(
            hardware=agent_pb2.HardwareSummary(
                cpu_model="t",
                cpu_cores=2,
                cpu_threads=2,
                ram_bytes=8 * GIB,
                capabilities=CAPS,
            )
        )
        fake.start_heartbeats(HB_S / 2)
        try:
            job_id = await _submit(runtime, http, tmp_path, items=4, chunk=2)
            start = None
            for _ in range(400):
                found = [
                    m.start_attempt
                    for m in fake.received
                    if m.WhichOneof("payload") == "start_attempt"
                ]
                if found:
                    start = found[0]
                    break
                await asyncio.sleep(0.01)
            assert start is not None

            async def send(**payload: Any) -> None:
                await fake.write(
                    agent_pb2.AgentMessage(
                        message_id=new_command_id(),
                        session_id=fake.session_id,
                        **payload,
                    )
                )

            seen = len(fake.received)
            await send(
                chunk_request=agent_pb2.ChunkRequest(
                    job_id=start.job_id,
                    attempt_id=start.attempt_id,
                    fencing_token=start.fencing_token,
                    want=1,
                )
            )
            grant = None
            for _ in range(400):
                for m in fake.received[seen:]:
                    if m.WhichOneof("payload") == "chunk_grant":
                        grant = m.chunk_grant.chunks[0]
                if grant is not None:
                    break
                await asyncio.sleep(0.01)
            assert grant is not None
            data = b"x"
            digest = sha256_bytes(data)
            async with api_client(
                runtime, token=start.spec.upload_token, node_id=fake.node_id
            ) as up:
                put = await up.put(f"/v1/blobs/{digest.hex}", content=data)
                assert put.status_code in (200, 201), put.text
            assert runtime.dispatcher is not None
            stats = runtime.dispatcher.split.stats
            await send(
                chunk_result=agent_pb2.ChunkResult(
                    job_id=start.job_id,
                    attempt_id=start.attempt_id,
                    fencing_token=start.fencing_token + 7,  # stale / forged
                    chunk_index=grant.chunk_index,
                    item_id=grant.item_id,
                    lease_seq=grant.lease_seq,
                    outcome=agent_pb2.ChunkResult.OUTCOME_DONE,
                    result_digest=str(digest),
                )
            )
            for _ in range(200):
                if stats.fenced.get("stale_token"):
                    break
                await asyncio.sleep(0.02)
            assert stats.fenced.get("stale_token") == 1, stats.fenced
            assert stats.committed == 0
            rows = await _sql(
                runtime,
                "SELECT i.state FROM work_item i JOIN work_queue q "
                "ON q.id = i.queue_id WHERE q.owner_id = :job AND i.id = :item",
                job=job_id,
                item=grant.item_id,
            )
            assert [r.state for r in rows] == ["leased"]  # never committed
        finally:
            await fake.close()


async def test_lane_attempts_never_feed_whole_run_history(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    async with _lanes(runtime, [0.0, 0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=40, chunk=5)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
    fingerprint = job.scheduling.fingerprint
    assert fingerprint
    # The fake lanes report no phase durations; give every lane the measured
    # run phase a real agent reports, so the filter (not a skipped phase) is
    # what keeps them out.
    assert runtime._engine is not None
    from sqlalchemy import text

    async with runtime._engine.begin() as conn:
        changed = await conn.execute(
            text(
                "UPDATE attempt_phase p SET skipped = false, duration_ms = 1234 "
                "FROM attempt a WHERE a.id = p.attempt_id AND a.job_id = :job "
                "AND a.role = 'lane' AND p.phase = 'run'"
            ),
            {"job": job_id},
        )
        assert changed.rowcount >= 1
        await conn.execute(
            text("UPDATE attempt SET output_bytes = 99 WHERE job_id = :job"),
            {"job": job_id},
        )
    sessions = create_session_factory(runtime._engine)
    async with sessions() as db:
        history = await _history(db, [fingerprint], 20)
        outputs = await _output_history(db, [fingerprint], 20)
    hist = history.get(fingerprint)
    assert hist is None or not hist.by_node_s, hist
    assert not outputs.get(fingerprint), outputs
    # control: the same rows as role 'run' WOULD be history - the role filter
    # is what keeps lanes out
    async with runtime._engine.begin() as conn:
        await conn.execute(
            text("UPDATE attempt SET role = 'run' WHERE job_id = :job"),
            {"job": job_id},
        )
    async with sessions() as db:
        control = await _history(db, [fingerprint], 20)
    assert control.get(fingerprint) is not None
    assert control[fingerprint].by_node_s, control


async def test_the_database_refuses_two_live_lanes_of_a_job_on_one_node(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    async with _lanes(runtime, [5.0]) as lanes:  # slow: its lane stays live
        job_id = await _submit(runtime, http, tmp_path, items=10, chunk=5)
        node = lanes[0].agent.node_id
        for _ in range(400):
            live = await _sql(
                runtime,
                "SELECT fencing_token FROM attempt WHERE job_id = :job "
                "AND role = 'lane' AND state NOT IN ('done','failed','lost','fenced')",
                job=job_id,
            )
            if live:
                break
            await asyncio.sleep(0.01)
        assert live
        assert runtime._engine is not None
        with pytest.raises(IntegrityError, match="ux_attempt_live_lane"):
            async with runtime._engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO attempt (id, job_id, node_id, fencing_token, "
                        "state, role, created_at, updated_at) VALUES (:id, :job, "
                        ":node, 999, 'assigned', 'lane', now(), now())"
                    ),
                    {"id": str(new_attempt_id()), "job": job_id, "node": node},
                )
