"""V1.0 G10 (D445): a short lease loss no longer fails a running attempt.

Lab (L3, G9-D): the agent lost its lease under memory pressure, reconnected
8-17 s later still running the attempt - and the controller had already ended
it ``node_lost`` and told the agent ``CancelAttempt(not_live_on_controller)``:
15 minutes of work lost, twice. Now a lease expiry puts the node's RUNNING
attempts in a reconnect grace (``[controller.liveness] attempt_grace_s``).

Real controller (PostgreSQL, mTLS gRPC sessions, real dispatcher), agents
driven by hand (``FakeAgent``):

* drop + reconnect (new epoch, Reconcile lists the attempt with its token)
  within the grace -> the attempt goes on, never cancelled, never re-placed,
  and its job succeeds on that attempt;
* a lane (Mode 3) is held too, with its chunk leases kept through the grace;
* no reconnect within the grace -> today's path: ``LOST(node_lost,
  node_offline)`` and the job is retried; a late reconnect is told to release;
* a Reconcile with another fencing token does not resume it;
* a revocation never waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_fabric import session_channel
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent
from sqlalchemy import text

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(180),
]

HB_S = 0.2
BUNDLE = os.urandom(1500)
GRACE_LONG_S = 8.0
GRACE_SHORT_S = 1.0


def _settings(pg: DatabaseSettings, grace: float) -> Any:
    return g2_settings(
        pg,
        jobs={"dispatch_interval_s": 0.1},
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S,
            miss_allowance_s=0.1,
            lease_multiplier=3,
            attempt_grace_s=grace,
        ),
    )


@pytest_asyncio.fixture
async def rt_long(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(_settings(pg_settings, GRACE_LONG_S)) as rt:
        yield rt


@pytest_asyncio.fixture
async def rt_short(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(_settings(pg_settings, GRACE_SHORT_S)) as rt:
        yield rt


def _hw() -> agent_pb2.HardwareSummary:
    return agent_pb2.HardwareSummary(
        cpu_model="t",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * 1024**3,
        capabilities=["exec", "exec-subprocess"],
    )


def _msg(agent: FakeAgent, **payload: Any) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(), session_id=agent.session_id, **payload
    )


def _status(
    agent: FakeAgent, start: agent_pb2.StartAttempt, state: int, **kw: Any
) -> agent_pb2.AgentMessage:
    return _msg(
        agent,
        attempt_status=agent_pb2.AttemptStatus(
            job_id=start.job_id,
            attempt_id=start.attempt_id,
            fencing_token=start.fencing_token,
            state=state,
            **kw,
        ),
    )


def _reconcile(
    agent: FakeAgent, start: agent_pb2.StartAttempt, token: int | None = None
) -> agent_pb2.AgentMessage:
    return _msg(
        agent,
        reconcile=agent_pb2.Reconcile(
            running_attempts=[
                agent_pb2.AttemptStatus(
                    job_id=start.job_id,
                    attempt_id=start.attempt_id,
                    fencing_token=start.fencing_token if token is None else token,
                    state=common_pb2.ATTEMPT_STATE_RUNNING,
                )
            ]
        ),
    )


async def _until(check: Callable[[], Any], timeout_s: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while not await _value(check()):
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.05)


async def _value(got: Any) -> Any:
    return await got if asyncio.iscoroutine(got) else got


def _acked(agent: FakeAgent, mid: str) -> bool:
    return any(m.ack.acked_message_id == mid for m in agent.messages("ack"))


def _cancels(agent: FakeAgent) -> list[agent_pb2.CancelAttempt]:
    return [m.cancel_attempt for m in agent.messages("cancel_attempt")]


async def _sql(rt: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert rt._engine is not None
    async with rt._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _attempts(rt: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        rt,
        "SELECT id, state, failure_type, detail, fencing_token FROM attempt "
        "WHERE job_id = :j ORDER BY fencing_token",
        j=job_id,
    )


async def _running_job(
    rt: ControllerRuntime, agent: FakeAgent
) -> tuple[str, agent_pb2.StartAttempt]:
    digest = sha256_bytes(BUNDLE)
    async with api_client(rt, token=token_of(rt)) as client:
        put = await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code in (200, 201), put.text
        created = await client.post(
            "/v1/jobs",
            json={
                "manifest": {"name": "grace", "entrypoint": "python main.py"},
                "bundle_digest": str(digest),
            },
        )
        assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    await _until(lambda: bool(agent.messages("start_attempt")))
    (sm,) = agent.messages("start_attempt")
    await agent.write(_msg(agent, ack=agent_pb2.Ack(acked_message_id=sm.message_id)))
    running = _status(agent, sm.start_attempt, common_pb2.ATTEMPT_STATE_RUNNING)
    await agent.write(running)
    await _until(lambda: _acked(agent, running.message_id))
    return job_id, sm.start_attempt


async def _drop(rt: ControllerRuntime, agent: FakeAgent) -> None:
    """The agent's process stalls: no Goodbye, no heartbeats; the lease expires."""
    agent.stop_heartbeats()
    await agent.abort()
    assert rt.dispatcher is not None
    dispatcher = rt.dispatcher
    await _until(lambda: dispatcher.stats.attempts_held >= 1)


async def test_a_reconnect_within_the_grace_keeps_the_attempt(
    rt_long: ControllerRuntime,
) -> None:
    rt = rt_long
    node = str(new_node_id())
    async with session_channel(rt, node) as channel:
        agent = FakeAgent(channel, node_id=node)
        back: FakeAgent | None = None
        try:
            await agent.connect(hardware=_hw())
            agent.start_heartbeats(HB_S)
            job_id, start = await _running_job(rt, agent)
            await _drop(rt, agent)
            ((_id, state, *_),) = await _attempts(rt, job_id)
            assert state == "running"  # held, not lost

            back = FakeAgent(channel, node_id=node, epoch=2)
            await back.connect(hardware=_hw())
            back.start_heartbeats(HB_S)
            await back.write(_reconcile(back, start))
            assert rt.dispatcher is not None
            dispatcher = rt.dispatcher
            await _until(lambda: dispatcher.stats.attempts_resumed == 1)
            await asyncio.sleep(GRACE_LONG_S + 1.0)  # well past the grace
            rows = await _attempts(rt, job_id)
            assert [(r.state, r.fencing_token) for r in rows] == [
                ("running", start.fencing_token)
            ]  # never lost, never re-placed
            assert _cancels(back) == []
            done = _status(back, start, common_pb2.ATTEMPT_STATE_DONE, exit_code=0)
            await back.write(done)
            await _until(lambda: _acked(back, done.message_id))
            async with api_client(rt, token=token_of(rt)) as client:
                body = (await client.get(f"/v1/jobs/{job_id}")).json()
            assert body["state"] == "succeeded", body
            assert dispatcher.stats.attempts_lost == 0
        finally:
            await agent.close()
            if back is not None:
                await back.close()


async def test_no_reconnect_within_the_grace_loses_and_retries(
    rt_short: ControllerRuntime,
) -> None:
    rt = rt_short
    node = str(new_node_id())
    async with session_channel(rt, node) as channel:
        agent = FakeAgent(channel, node_id=node)
        late: FakeAgent | None = None
        try:
            await agent.connect(hardware=_hw())
            agent.start_heartbeats(HB_S)
            job_id, start = await _running_job(rt, agent)
            await _drop(rt, agent)

            async def lost() -> bool:
                rows = await _attempts(rt, job_id)
                return rows[0].state == "lost"

            await _until(lost)
            first = (await _attempts(rt, job_id))[0]
            assert (first.failure_type, first.detail) == ("node_lost", "node_offline")
            history = await _sql(
                rt,
                "SELECT to_state FROM job_transition WHERE job_id = :j",
                j=job_id,
            )
            assert "retrying" in {h.to_state for h in history}  # retried

            # the node comes back after the grace: told to release it
            late = FakeAgent(channel, node_id=node, epoch=2)
            await late.connect(hardware=_hw())
            late.start_heartbeats(HB_S)
            await late.write(_reconcile(late, start))
            await _until(lambda: bool(_cancels(late)))
            (cancel,) = _cancels(late)
            assert cancel.attempt_id == start.attempt_id
            assert cancel.reason == "not_live_on_controller" and cancel.release
        finally:
            await agent.close()
            if late is not None:
                await late.close()


async def test_a_reconcile_with_another_token_does_not_resume(
    rt_long: ControllerRuntime,
) -> None:
    rt = rt_long
    node = str(new_node_id())
    async with session_channel(rt, node) as channel:
        agent = FakeAgent(channel, node_id=node)
        back: FakeAgent | None = None
        try:
            await agent.connect(hardware=_hw())
            agent.start_heartbeats(HB_S)
            job_id, start = await _running_job(rt, agent)
            await _drop(rt, agent)
            back = FakeAgent(channel, node_id=node, epoch=2)
            await back.connect(hardware=_hw())
            back.start_heartbeats(HB_S)
            # fencing: the node holds the attempt id with a stale token
            await back.write(_reconcile(back, start, token=start.fencing_token + 7))

            async def lost() -> bool:
                return (await _attempts(rt, job_id))[0].state == "lost"

            await _until(lost)  # acked start, not held with its token -> LOST
            first = (await _attempts(rt, job_id))[0]
            assert first.detail == "agent_lost_attempt"
            assert rt.dispatcher is not None
            assert rt.dispatcher.stats.attempts_resumed == 0
        finally:
            await agent.close()
            if back is not None:
                await back.close()


async def test_a_revoked_node_never_waits(rt_long: ControllerRuntime) -> None:
    rt = rt_long
    node = str(new_node_id())
    async with session_channel(rt, node) as channel:
        agent = FakeAgent(channel, node_id=node)
        try:
            await agent.connect(hardware=_hw())
            agent.start_heartbeats(HB_S)
            job_id, _start = await _running_job(rt, agent)
            async with api_client(rt, token=token_of(rt)) as client:
                got = await client.post(f"/v1/nodes/{node}/revoke")
                assert got.status_code == 200, got.text

            async def lost() -> bool:
                return (await _attempts(rt, job_id))[0].state == "lost"

            await _until(lost, timeout_s=GRACE_LONG_S - 2.0)  # well inside it
            first = (await _attempts(rt, job_id))[0]
            assert (first.failure_type, first.detail) == ("node_lost", "node_revoked")
            assert rt.dispatcher is not None
            assert rt.dispatcher.stats.attempts_held == 0
        finally:
            await agent.close()


async def test_a_held_lane_keeps_its_chunk_leases(
    rt_long: ControllerRuntime, tmp_path: Any
) -> None:
    from mf_fabric import NodeChannels
    from mf_fake_lanes import FakeLane

    from meshforge.common.tree import build_tree

    rt = rt_long
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_bytes(b"print(1)\n")
    built = build_tree(root)
    assert rt.blobs is not None
    rt.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        rt.blobs.cas.put_file(path)
    async with NodeChannels(f"127.0.0.1:{rt.grpc_port}") as channels:
        fake = FakeAgent(channels)
        await fake.connect(
            hardware=agent_pb2.HardwareSummary(
                cpu_model="t",
                cpu_cores=4,
                cpu_threads=8,
                ram_bytes=16 * 1024**3,
                capabilities=["exec", "exec-subprocess", "exec-split"],
            ),
            max_cpu_jobs=2,
        )
        fake.start_heartbeats(HB_S)
        lane = FakeLane(fake, rt, speed_s=0.0, hold_results=True)  # holds a lease
        lane.start()
        back: FakeAgent | None = None
        try:
            async with api_client(rt, token=token_of(rt)) as http:
                got = await http.post(
                    "/v1/jobs",
                    json={
                        "manifest": {
                            "name": "grace-lane",
                            "entrypoint": "python main.py {items_file} {out}",
                            "split": {
                                "over": {"range": {"stop": 20}},
                                "chunk": {"items": 2},
                                "speculative": False,
                            },
                        },
                        "tree_id": str(built.tree.tree_id),
                        "placement": {},
                    },
                )
                assert got.status_code == 201, got.text
                job_id = got.json()["job_id"]
            await _until(lambda: bool(lane.ledger))  # a chunk leased and "run"
            (start_msg,) = [
                m
                for m in fake.messages("start_attempt")
                if m.start_attempt.HasField("lane")
            ]
            start = start_msg.start_attempt
            await lane.close()
            await _drop(rt, fake)
            leases = await _sql(
                rt,
                "SELECT deadline - now() AS left FROM work_lease "
                "WHERE holder = :a AND state = 'live'",
                a=start.attempt_id,
            )
            assert leases, "the lane's lease went back to the queue"
            assert all(r.left.total_seconds() > GRACE_LONG_S for r in leases)

            back = FakeAgent(channels, node_id=fake.node_id, epoch=2)
            await back.connect(hardware=_hw())
            back.start_heartbeats(HB_S)
            await back.write(_reconcile(back, start))
            assert rt.dispatcher is not None
            dispatcher = rt.dispatcher
            await _until(lambda: dispatcher.stats.attempts_resumed == 1)
            rows = await _sql(
                rt,
                "SELECT state FROM attempt WHERE id = :a",
                a=start.attempt_id,
            )
            assert [r.state for r in rows] == ["running"]
            assert _cancels(back) == []
            del job_id
        finally:
            await lane.close()
            with contextlib.suppress(Exception):
                await fake.close()
            if back is not None:
                await back.close()
