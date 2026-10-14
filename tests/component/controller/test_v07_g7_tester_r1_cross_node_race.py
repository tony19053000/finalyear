"""V0.7 G7 tester r1 (CG-E2): wrong-node reports against retried / ended attempts,
and over a real mTLS session (Reconcile claim, reconnect replay).

Real PostgreSQL; the e2e part uses the real mTLS controller and hand-driven agents.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from mf_reports import apply_as_placed
from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchDecision,
    ExecutionStore,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.queue_reads import FabricRows
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

DIGEST = "sha256:" + "ab" * 32
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _nodes(pg: DatabaseSettings, count: int) -> list[str]:
    engine = create_engine(pg)
    try:
        ids = []
        for _ in range(count):
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
                cpu_slots=4,
                gpu_slots=0,
            )
            ids.append(node)
        return ids
    finally:
        await engine.dispose()


class OnNode:
    def __init__(self, node: str) -> None:
        self.node = node

    def __call__(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        return Dispatch(
            attempt_id=str(new_attempt_id()), start_snapshot={}, node_id=self.node
        )


async def _tick(store: ExecutionStore, decide: Any) -> list[Any]:
    return await store.dispatch_planned(
        decide,
        limit=10,
        max_per_node=8,
        now=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5),
        aging_s=600.0,
    )


async def _job_on(store: ExecutionStore, node: str, **sched: Any) -> Any:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(retry_backoff_s=0, **sched),
    )
    (res,) = [r for r in await _tick(store, OnNode(node)) if r.job.id == job.id]
    out = await apply_as_placed(
        store,
        StatusReport(res.attempt.id, res.attempt.fencing_token, AttemptState.RUNNING),
    )
    assert out.kind is StatusKind.APPLIED
    return job, out.attempt


async def test_wrong_node_after_reassignment_never_moves_either_attempt(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node_a, node_b, node_c = await _nodes(pg_settings, 3)
    job, first = await _job_on(store, node_a)
    lost = await store.end_attempt(
        first.id,
        first.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="node_offline",
    )
    assert lost.job is not None and lost.job.state is JobState.QUEUED
    (res,) = [r for r in await _tick(store, OnNode(node_b)) if r.job.id == job.id]
    second = res.attempt
    assert second.node_id == node_b and second.fencing_token == 2
    await apply_as_placed(
        store, StatusReport(second.id, second.fencing_token, AttemptState.RUNNING)
    )

    async def snapshot() -> Any:
        rows = await _sql(
            pg_settings,
            "SELECT id, state, fencing_token, failure_type FROM attempt "
            "WHERE job_id = :j ORDER BY created_at, id",
            j=job.id,
        )
        j = await store.get_job(job.id)
        assert j is not None
        return rows, j.state, j.attempt_epoch, await store.transitions(job.id)

    before = await snapshot()
    retryable_fail = {
        "failure_type": FailureType.INTEGRITY_ERROR,
        "detail": "forged",
        "retryable": True,
    }
    # C (a third enrolled node) forges against both attempts, any token;
    # A (the OLD owner) forges against B's live attempt; B against A's dead one
    # carrying the NEW token (the very token it legitimately holds).
    forgeries: list[tuple[str, str, int, AttemptState, dict[str, Any]]] = []
    for who in (node_c, node_a):
        for attempt in (first, second):
            for token in (1, 2, 99):
                forgeries.append(
                    (who, attempt.id, token, AttemptState.FAILED, retryable_fail)
                )
                forgeries.append((who, attempt.id, token, AttemptState.DONE, {}))
                forgeries.append((who, attempt.id, token, AttemptState.UPLOADING, {}))
    forgeries.append((node_b, first.id, 1, AttemptState.DONE, {}))
    forgeries.append((node_b, first.id, 2, AttemptState.FAILED, retryable_fail))
    forgeries = [f for f in forgeries if not (f[0] == node_a and f[1] == first.id)]
    forgeries = [f for f in forgeries if not (f[0] == node_b and f[1] == second.id)]
    assert len(forgeries) > 25
    for who, attempt_id, token, state, extra in forgeries:
        out = await store.apply_status(
            StatusReport(attempt_id, token, state, **extra), node_id=who
        )
        assert out.kind is StatusKind.WRONG_NODE, (who, attempt_id, token, state)
        assert out.attempt is None and out.job is None and out.retry is None
        assert not out.ended
    assert await snapshot() == before

    # The honest owners still behave exactly as before: the old owner's late
    # report is FENCED (nothing applied), the new owner finishes the job.
    late = await store.apply_status(
        StatusReport(first.id, 1, AttemptState.DONE), node_id=node_a
    )
    assert late.kind is StatusKind.FENCED
    done = await apply_as_placed(store, StatusReport(second.id, 2, AttemptState.DONE))
    assert done.kind is StatusKind.APPLIED and done.ended
    j = await store.get_job(job.id)
    assert j is not None and j.state is JobState.SUCCEEDED


@pytest.mark.parametrize("final", ["done", "failed"])
async def test_wrong_node_on_an_ended_attempt_is_still_wrong_node_not_duplicate(
    store: ExecutionStore, pg_settings: DatabaseSettings, final: str
) -> None:
    node_a, node_b = await _nodes(pg_settings, 2)
    job, attempt = await _job_on(store, node_a, max_attempts=1)
    if final == "done":
        report = StatusReport(attempt.id, attempt.fencing_token, AttemptState.DONE)
    else:
        report = StatusReport(
            attempt.id,
            attempt.fencing_token,
            AttemptState.FAILED,
            failure_type=FailureType.USER_ERROR,
            detail="x",
        )
    assert (await apply_as_placed(store, report)).kind is StatusKind.APPLIED
    before = await store.transitions(job.id)
    out = await store.apply_status(report, node_id=node_b)
    assert out.kind is StatusKind.WRONG_NODE
    again = await store.apply_status(report, node_id=node_a)
    assert again.kind is StatusKind.DUPLICATE
    assert await store.transitions(job.id) == before


# ------------------------------------------------------- e2e (real mTLS controller)

import os  # noqa: E402
from collections.abc import AsyncIterator as _AsyncIterator  # noqa: E402

from mf_fabric import session_channel  # noqa: E402
from mf_jobs_harness import (  # noqa: E402
    api_client,
    g2_settings,
    running_controller,
    token_of,
)
from mf_session_client import FakeAgent  # noqa: E402

from meshforge.common.hashing import sha256_bytes  # noqa: E402
from meshforge.common.ids import new_command_id  # noqa: E402
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2  # noqa: E402
from meshforge.controller.server import ControllerRuntime  # noqa: E402

BUNDLE = os.urandom(1500)


@pytest_asyncio.fixture
async def rt(pg_settings: DatabaseSettings) -> _AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 60.0})
    async with running_controller(settings) as runtime:
        yield runtime


def _hw(*caps: str) -> agent_pb2.HardwareSummary:
    return agent_pb2.HardwareSummary(
        cpu_model="t",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * 1024**3,
        capabilities=list(caps),
    )


def _st(
    agent: FakeAgent, start: Any, state: int, message_id: str | None = None, **kw: Any
) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=message_id or new_command_id(),
        session_id=agent.session_id,
        attempt_status=agent_pb2.AttemptStatus(
            job_id=start.job_id,
            attempt_id=start.attempt_id,
            fencing_token=start.fencing_token,
            state=state,
            **kw,  # type: ignore[arg-type]
        ),
    )


async def _until(check: Any, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while not check():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.02)


def _acked(agent: FakeAgent, mid: str) -> bool:
    return any(m.ack.acked_message_id == mid for m in agent.messages("ack"))


async def test_reconcile_claim_and_reconnect_replay_from_node_b_move_nothing(
    rt: ControllerRuntime,
) -> None:
    digest = sha256_bytes(BUNDLE)
    async with api_client(rt, token=token_of(rt)) as client:
        assert (
            await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        ).status_code == 201
    node_a, node_b = str(new_node_id()), str(new_node_id())
    async with (
        session_channel(rt, node_a) as ch_a,
        session_channel(rt, node_b) as ch_b,
    ):
        a = FakeAgent(ch_a, node_id=node_a)
        b = FakeAgent(ch_b, node_id=node_b)
        b2: FakeAgent | None = None
        try:
            await a.connect(hardware=_hw("exec", "exec-subprocess"))
            await b.connect(hardware=_hw())
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
                assert rt.dispatcher is not None
                rt.dispatcher.wake()
                await _until(lambda: bool(a.messages("start_attempt")))
                (sm,) = a.messages("start_attempt")
                start = sm.start_attempt
                await a.write(
                    agent_pb2.AgentMessage(
                        message_id=new_command_id(),
                        session_id=a.session_id,
                        ack=agent_pb2.Ack(acked_message_id=sm.message_id),
                    )
                )
                running = _st(a, start, common_pb2.ATTEMPT_STATE_RUNNING)
                await a.write(running)
                await _until(lambda: _acked(a, running.message_id))
                a_cmds = len(a.received)

                # B claims A's attempt in a Reconcile, then forges final reports.
                await b.write(
                    agent_pb2.AgentMessage(
                        message_id=new_command_id(),
                        session_id=b.session_id,
                        reconcile=agent_pb2.Reconcile(
                            running_attempts=[
                                agent_pb2.AttemptStatus(
                                    job_id=start.job_id,
                                    attempt_id=start.attempt_id,
                                    fencing_token=start.fencing_token,
                                    state=common_pb2.ATTEMPT_STATE_RUNNING,
                                )
                            ]
                        ),
                    )
                )
                forged_ids = []
                for state in (
                    common_pb2.ATTEMPT_STATE_FAILED,
                    common_pb2.ATTEMPT_STATE_DONE,
                ):
                    kw: dict[str, Any] = {}
                    if state == common_pb2.ATTEMPT_STATE_FAILED:
                        kw["failure"] = common_pb2.Failure(
                            failure_type=common_pb2.FAILURE_TYPE_INTEGRITY_ERROR,
                            message="forged",
                            retryable=True,
                        )
                    m = _st(b, start, state, **kw)
                    forged_ids.append((m.message_id, state, kw))
                    await b.write(m)
                    await _until(lambda mid=m.message_id: _acked(b, mid))
                before = (await client.get(f"/v1/jobs/{job_id}")).json()
                assert before["state"] == "running", before

                # B drops and reconnects with a higher epoch and REPLAYS the same ids.
                await b.abort()
                b2 = FakeAgent(ch_b, node_id=node_b, epoch=2)
                await b2.connect(hardware=_hw())
                b2.start_heartbeats(0.3)
                for mid, state, kw in forged_ids:
                    await b2.write(_st(b2, start, state, message_id=mid, **kw))
                m2 = _st(b2, start, common_pb2.ATTEMPT_STATE_DONE)
                await b2.write(m2)
                await _until(lambda: _acked(b2, m2.message_id))
                await asyncio.sleep(0.5)

                assert rt.dispatcher.stats.statuses_wrong_node >= 3
                body = (await client.get(f"/v1/jobs/{job_id}")).json()
                assert body["state"] == "running", body
                # A was never told to cancel / release, and still finishes the job.
                assert not [
                    m
                    for m in a.received[a_cmds:]
                    if m.WhichOneof("payload") in ("cancel_attempt", "release_attempt")
                ]
                done = _st(a, start, common_pb2.ATTEMPT_STATE_DONE, exit_code=0)
                await a.write(done)
                await _until(lambda: _acked(a, done.message_id))
                final = (await client.get(f"/v1/jobs/{job_id}")).json()
                assert final["state"] == "succeeded", final
        finally:
            await a.close()
            await b.close()
            if b2 is not None:
                await b2.close()
