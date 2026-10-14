"""V0.7 G7 (THREAT_MODEL CG-E2): an enrolled node B cannot report for an
attempt placed on node A.

Threat: T3 (a stolen or compromised laptop with a valid node certificate)
learns another node's attempt id and fencing token - the token is a small
epoch counter, the ids are visible to the job's owner - and sends
``AttemptStatus`` / ``LogChunk`` for it: to fail someone else's job, to make
it ``SUCCEEDED`` early, to trigger a tree scrub (``integrity_error``), or to
write its own bytes into another job's log.

Defence: the reporting node is the one its session's client certificate
proves (G3); ``ExecutionStore.apply_status`` refuses a report whose node is not
the attempt's (``WRONG_NODE``, before the token or the state is read), the
dispatcher acks it (no replay), logs ``controller.jobs.status_wrong_node``
(``reason=wrong_node``) and does nothing else - no cancel to B, no retry, no
scrub; ``LogHub.on_chunk`` drops a chunk from the wrong node (``chunks_fenced``).
Outputs: an upload under A's grant needs A's client certificate
(CB-S1, ``tests/component/controller/test_v07_g3_https.py``).

Real PostgreSQL; a real mTLS controller with two hand-driven agents.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import structlog
from alembic import command
from mf_fabric import session_channel
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

from meshforge.common.errors import FailureType
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import (
    new_attempt_id,
    new_command_id,
    new_job_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    ExecutionStore,
    NodeView,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

GIB = 1024**3
BUNDLE = os.urandom(2000)
DIGEST = "sha256:" + "ab" * 32
MANIFEST: dict[str, Any] = {
    "name": "demo",
    "entrypoint": ["python", "main.py"],
    "limits": {"timeout": 60, "network": "default"},
}


# ------------------------------------------------------------ store level


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=10)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _node(pg_settings: DatabaseSettings) -> str:
    engine = create_engine(pg_settings)
    try:
        node = str(new_node_id())
        await Registry(engine).upsert_node_on_hello(
            node_id=node,
            hostname="h",
            os="linux",
            platform="x86_64",
            hardware={"capabilities": ["exec", "exec-subprocess"]},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
        )
        return node
    finally:
        await engine.dispose()


def _dispatch_all(_job: JobRecord, node: NodeView | None) -> Dispatch | None:
    if node is None:
        return None
    return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={"k": 1})


async def test_the_store_refuses_every_report_from_another_node_and_changes_nothing(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node_a = await _node(pg_settings)
    node_b = await _node(pg_settings)
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node_a},
        request_hash="h" * 64,
        idempotency_key=None,
    )
    (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=4)
    assert result.attempt is not None and result.attempt.node_id == node_a
    attempt_id, token = result.attempt.id, result.attempt.fencing_token
    assert await store.mark_start_acked(attempt_id, token)
    running = await store.apply_status(
        StatusReport(attempt_id, token, AttemptState.RUNNING), node_id=node_a
    )
    assert running.kind is StatusKind.APPLIED
    history = await store.transitions(job.id)

    forged = [
        StatusReport(attempt_id, token, AttemptState.UPLOADING),
        StatusReport(attempt_id, token, AttemptState.DONE, output_digest=DIGEST),
        StatusReport(
            attempt_id,
            token,
            AttemptState.FAILED,
            failure_type=FailureType.INTEGRITY_ERROR,
            detail="forged",
            retryable=True,
        ),
        # A wrong token from the wrong node is refused as wrong_node too: B
        # learns nothing about the token from the outcome.
        StatusReport(attempt_id, token + 5, AttemptState.DONE),
    ]
    for report in forged:
        outcome = await store.apply_status(report, node_id=node_b)
        assert outcome.kind is StatusKind.WRONG_NODE, report
        assert outcome.attempt is None and outcome.job is None and not outcome.ended

    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.RUNNING
    assert attempt.failure_type is None and attempt.detail is None
    stored_job = await store.get_job(job.id)
    assert stored_job is not None and stored_job.state is JobState.RUNNING
    assert await store.transitions(job.id) == history
    assert await store.phases(attempt_id) == []

    # The owner still controls it.
    done = await store.apply_status(
        StatusReport(attempt_id, token, AttemptState.DONE), node_id=node_a
    )
    assert done.kind is StatusKind.APPLIED and done.ended


# ------------------------------------------------------------ end to end


@pytest_asyncio.fixture
async def rt(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 60.0})
    async with running_controller(settings) as runtime:
        yield runtime


def _hardware(*capabilities: str) -> agent_pb2.HardwareSummary:
    return agent_pb2.HardwareSummary(
        cpu_model="t",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * GIB,
        capabilities=list(capabilities),
    )


def _status(
    agent: FakeAgent, start: agent_pb2.StartAttempt, state: int, **fields: Any
) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=agent.session_id,
        attempt_status=agent_pb2.AttemptStatus(
            job_id=start.job_id,
            attempt_id=start.attempt_id,
            fencing_token=start.fencing_token,
            state=state,  # type: ignore[arg-type]
            **fields,
        ),
    )


def _chunk(
    agent: FakeAgent, start: agent_pb2.StartAttempt, offset: int, data: bytes
) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=agent.session_id,
        log_chunk=agent_pb2.LogChunk(
            attempt_id=start.attempt_id,
            job_id=start.job_id,
            stream=agent_pb2.LogChunk.STREAM_STDOUT,
            offset=offset,
            data=data,
            fencing_token=start.fencing_token,
        ),
    )


async def _until(check: Any, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while not check():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.02)


def _acked(agent: FakeAgent, message_id: str) -> bool:
    return any(m.ack.acked_message_id == message_id for m in agent.messages("ack"))


async def test_node_b_cannot_report_status_or_logs_for_node_as_attempt(
    rt: ControllerRuntime,
) -> None:
    digest = sha256_bytes(BUNDLE)
    async with api_client(rt, token=token_of(rt)) as client:
        put = await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code == 201, put.text
    node_a, node_b = str(new_node_id()), str(new_node_id())
    async with (
        session_channel(rt, node_a) as channel_a,
        session_channel(rt, node_b) as channel_b,
    ):
        a = FakeAgent(channel_a, node_id=node_a)
        b = FakeAgent(channel_b, node_id=node_b)
        try:
            await a.connect(hardware=_hardware("exec", "exec-subprocess"))
            await b.connect(hardware=_hardware())  # online, never placed
            a.start_heartbeats(0.3)
            b.start_heartbeats(0.3)
            async with api_client(rt, token=token_of(rt)) as client:
                created = await client.post(
                    "/v1/jobs",
                    json={
                        "manifest": {"name": "demo", "entrypoint": "python main.py"},
                        "bundle_digest": str(digest),
                    },
                )
                assert created.status_code == 201, created.text
                job_id = created.json()["job_id"]
                assert rt.dispatcher is not None and rt.logs is not None
                rt.dispatcher.wake()
                await _until(lambda: bool(a.messages("start_attempt")), timeout_s=10)
                (start_msg,) = a.messages("start_attempt")
                start = start_msg.start_attempt
                await a.write(
                    agent_pb2.AgentMessage(
                        message_id=new_command_id(),
                        session_id=a.session_id,
                        ack=agent_pb2.Ack(acked_message_id=start_msg.message_id),
                    )
                )
                running = _status(a, start, common_pb2.ATTEMPT_STATE_RUNNING)
                await a.write(running)
                await _until(lambda: _acked(a, running.message_id), timeout_s=10)
                await a.write(_chunk(a, start, 0, b"from-a\n"))
                await _until(
                    lambda: rt.logs is not None and rt.logs.stats.chunks_appended >= 1,
                    timeout_s=10,
                )
                before = await client.get(f"/v1/jobs/{job_id}")
                assert before.json()["state"] == "running", before.text
                fenced_before = rt.logs.stats.chunks_fenced
                commands_to_b = len(b.received)

                with structlog.testing.capture_logs() as logs:
                    forged = [
                        _status(
                            b,
                            start,
                            common_pb2.ATTEMPT_STATE_FAILED,
                            failure=common_pb2.Failure(
                                failure_type=common_pb2.FAILURE_TYPE_INTEGRITY_ERROR,
                                message="forged by b",
                                retryable=True,
                            ),
                        ),
                        _status(b, start, common_pb2.ATTEMPT_STATE_UPLOADING),
                        _status(
                            b,
                            start,
                            common_pb2.ATTEMPT_STATE_DONE,
                            output_digest="sha256:" + "cd" * 32,
                        ),
                        _status(b, start, common_pb2.ATTEMPT_STATE_DONE),
                    ]
                    for message in forged:
                        await b.write(message)
                        await _until(
                            lambda m=message: _acked(b, m.message_id), timeout_s=10
                        )
                    await b.write(_chunk(b, start, 7, b"from-b\n"))
                    await _until(
                        lambda: (
                            rt.logs is not None
                            and rt.logs.stats.chunks_fenced == fenced_before + 1
                        ),
                        timeout_s=10,
                    )

                # Refused, with its reason code, and nothing followed from it.
                assert rt.dispatcher.stats.statuses_wrong_node == len(forged)
                refused = [
                    e for e in logs if e["event"] == "controller.jobs.status_wrong_node"
                ]
                assert len(refused) == len(forged)
                assert {e["reason"] for e in refused} == {"wrong_node"}
                assert {e["node_id"] for e in refused} == {node_b}
                assert {e["attempt_id"] for e in refused} == {start.attempt_id}
                # B got its acks and nothing else (no cancel/release for A's attempt).
                assert {
                    m.WhichOneof("payload") for m in b.received[commands_to_b:]
                } <= {"ack", "ping"}
                # Nothing persisted: the job and the attempt are as A left them.
                after = await client.get(f"/v1/jobs/{job_id}")
                body = after.json()
                assert body["state"] == "running", body
                logs_now = await client.get(f"/v1/jobs/{job_id}/logs?stream=stdout")
                assert logs_now.content == b"from-a\n"

                # A still owns it: its DONE ends the job.
                done = _status(a, start, common_pb2.ATTEMPT_STATE_DONE, exit_code=0)
                await a.write(done)
                await _until(lambda: _acked(a, done.message_id), timeout_s=10)
                final = await client.get(f"/v1/jobs/{job_id}")
                assert final.json()["state"] == "succeeded", final.text
        finally:
            await a.close()
            await b.close()
