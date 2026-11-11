"""V1.0 G10 (D445) tester: adversarial reconnect-grace scenarios.

Real controller on PostgreSQL, mTLS sessions, hand-driven ``FakeAgent`` /
``FakeLane``. Derived from the D445 contract (progress file G10-B, ARCHITECTURE
12.2 amendment), not from the implementation:

* a reconnect shortly before / after the grace edge;
* a reconnect that does not list the attempt (agent restarted) never waits;
* a controller restart during the grace;
* cancel / job timeout / revoke during the grace;
* split lanes: chunk leases kept while held, fencing after the grace;
* a second loss inside (and after) the grace;
* what the API shows during the grace.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_fabric import NodeChannels, session_channel
from mf_fake_lanes import FakeLane
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent
from sqlalchemy import text

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]

HB_S = 0.2
BUNDLE = os.urandom(1200)


def _settings(
    pg: DatabaseSettings, grace: float, jobs: dict[str, Any] | None = None, **ctl: Any
) -> Any:
    return g2_settings(
        pg,
        jobs={"dispatch_interval_s": 0.1, **(jobs or {})},
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S,
            miss_allowance_s=0.1,
            lease_multiplier=3,
            attempt_grace_s=grace,
        ),
        **ctl,
    )


@contextlib.asynccontextmanager
async def _rt(
    pg: DatabaseSettings, grace: float, jobs: dict[str, Any] | None = None, **ctl: Any
) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(_settings(pg, grace, jobs, **ctl)) as rt:
        yield rt


def _hw(split: bool = False) -> agent_pb2.HardwareSummary:
    caps = ["exec", "exec-subprocess"] + (["exec-split"] if split else [])
    return agent_pb2.HardwareSummary(
        cpu_model="t",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * 1024**3,
        capabilities=caps,
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
    agent: FakeAgent, start: agent_pb2.StartAttempt | None, token: int | None = None
) -> agent_pb2.AgentMessage:
    running = []
    if start is not None:
        running.append(
            agent_pb2.AttemptStatus(
                job_id=start.job_id,
                attempt_id=start.attempt_id,
                fencing_token=start.fencing_token if token is None else token,
                state=common_pb2.ATTEMPT_STATE_RUNNING,
            )
        )
    return _msg(agent, reconcile=agent_pb2.Reconcile(running_attempts=running))


async def _value(got: Any) -> Any:
    return await got if asyncio.iscoroutine(got) else got


async def _until(check: Callable[[], Any], timeout_s: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while not await _value(check()):
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.05)


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


async def _job(rt: ControllerRuntime, job_id: str) -> dict[str, Any]:
    async with api_client(rt, token=token_of(rt)) as client:
        got = await client.get(f"/v1/jobs/{job_id}")
        assert got.status_code == 200, got.text
        return dict(got.json())


async def _submit(rt: ControllerRuntime, manifest: dict[str, Any] | None = None) -> str:
    digest = sha256_bytes(BUNDLE)
    body = {"name": "grace", "entrypoint": "python main.py", **(manifest or {})}
    async with api_client(rt, token=token_of(rt)) as client:
        put = await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code in (200, 201), put.text
        created = await client.post(
            "/v1/jobs", json={"manifest": body, "bundle_digest": str(digest)}
        )
        assert created.status_code == 201, created.text
    return str(created.json()["job_id"])


async def _running(
    agent: FakeAgent, rt: ControllerRuntime, manifest: dict[str, Any] | None = None
) -> tuple[str, agent_pb2.StartAttempt]:
    job_id = await _submit(rt, manifest)
    await _until(lambda: bool(agent.messages("start_attempt")))
    sm = agent.messages("start_attempt")[-1]
    await agent.write(_msg(agent, ack=agent_pb2.Ack(acked_message_id=sm.message_id)))
    running = _status(agent, sm.start_attempt, common_pb2.ATTEMPT_STATE_RUNNING)
    await agent.write(running)
    await _until(lambda: _acked(agent, running.message_id))
    return job_id, sm.start_attempt


async def _drop(rt: ControllerRuntime, agent: FakeAgent, held: int = 1) -> None:
    """Stall: no Goodbye, no heartbeats; the lease expires."""
    agent.stop_heartbeats()
    await agent.abort()
    assert rt.dispatcher is not None
    d = rt.dispatcher
    await _until(lambda: d.stats.attempts_held >= held)


async def _back(
    channel: Any,
    node: str,
    epoch: int,
    start: agent_pb2.StartAttempt | None,
    token: int | None = None,
    split: bool = False,
) -> FakeAgent:
    a = FakeAgent(channel, node_id=node, epoch=epoch)
    await a.connect(hardware=_hw(split))
    a.start_heartbeats(HB_S)
    await a.write(_reconcile(a, start, token))
    return a


def _until_edge(rt: ControllerRuntime, attempt_id: str) -> float:
    """Seconds left of the attempt's grace (dispatcher clock)."""
    d = rt.dispatcher
    assert d is not None
    return float(d._held[attempt_id].until - d._clock())


# ------------------------------------------------------------- edges


async def test_reconnect_one_second_before_the_edge_resumes_after_it_loses(
    pg_settings: DatabaseSettings,
) -> None:
    grace = 4.0
    async with _rt(pg_settings, grace) as rt:
        assert rt.dispatcher is not None
        d = rt.dispatcher
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            others: list[FakeAgent] = []
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, start = await _running(agent, rt)
                await _drop(rt, agent)
                # still held half-way through the grace
                await asyncio.sleep(grace / 2)
                assert (await _attempts(rt, job_id))[0].state == "running"
                await asyncio.sleep(max(0.0, _until_edge(rt, start.attempt_id) - 1.0))
                b = await _back(ch, node, 2, start)
                others.append(b)
                await _until(lambda: d.stats.attempts_resumed == 1)
                await asyncio.sleep(grace + 1.0)  # past the original edge
                rows = await _attempts(rt, job_id)
                assert [(r.state, r.fencing_token) for r in rows] == [
                    ("running", start.fencing_token)
                ]
                assert _cancels(b) == [] and d.stats.attempts_lost == 0
                done = _status(b, start, common_pb2.ATTEMPT_STATE_DONE, exit_code=0)
                await b.write(done)
                await _until(lambda: _acked(b, done.message_id))
                assert (await _job(rt, job_id))["state"] == "succeeded"
            finally:
                await agent.close()
                for o in others:
                    await o.close()


async def test_reconnect_one_second_after_the_edge_is_too_late(
    pg_settings: DatabaseSettings,
) -> None:
    grace = 3.0
    async with _rt(pg_settings, grace) as rt:
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            late: FakeAgent | None = None
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, start = await _running(agent, rt)
                t0 = time.monotonic()
                await _drop(rt, agent)
                await asyncio.sleep(max(0.0, _until_edge(rt, start.attempt_id) + 1.0))
                rows = await _attempts(rt, job_id)
                assert rows[0].state == "lost", rows
                assert (rows[0].failure_type, rows[0].detail) == (
                    "node_lost",
                    "node_offline",
                )
                # the grace was honoured: not lost before it ran out
                assert time.monotonic() - t0 >= grace
                late = await _back(ch, node, 2, start)
                await _until(lambda: bool(_cancels(late)))
                (c,) = _cancels(late)
                assert c.release and c.reason == "not_live_on_controller"
                # the retry ran as attempt 2 (not resumed on the old token)
                assert rt.dispatcher is not None
                assert rt.dispatcher.stats.attempts_resumed == 0
            finally:
                await agent.close()
                if late is not None:
                    await late.close()


async def test_reconnect_without_the_attempt_is_lost_at_once_not_after_the_grace(
    pg_settings: DatabaseSettings,
) -> None:
    async with _rt(pg_settings, 60.0) as rt:
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            back: FakeAgent | None = None
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, _start = await _running(agent, rt)
                await _drop(rt, agent)
                t0 = time.monotonic()
                back = await _back(ch, node, 2, None)  # restarted: nothing running
                await _until(
                    lambda: _lost(rt, job_id),
                    timeout_s=8.0,
                )
                assert time.monotonic() - t0 < 8.0  # nowhere near the 60 s grace
                first = (await _attempts(rt, job_id))[0]
                assert (first.failure_type, first.detail) == (
                    "node_lost",
                    "agent_lost_attempt",
                )
                assert rt.dispatcher is not None
                assert rt.dispatcher._held == {}
                assert rt.dispatcher.stats.attempts_resumed == 0
                hist = await _sql(
                    rt,
                    "SELECT to_state FROM job_transition WHERE job_id = :j",
                    j=job_id,
                )
                assert "retrying" in {h.to_state for h in hist}
            finally:
                await agent.close()
                if back is not None:
                    await back.close()


async def _lost(rt: ControllerRuntime, job_id: str) -> bool:
    return (await _attempts(rt, job_id))[0].state == "lost"


# ------------------------------------------------------------- restart


async def test_controller_restart_during_the_grace_the_node_still_continues(
    pg_settings: DatabaseSettings,
) -> None:
    settings = _settings(pg_settings, 60.0, jobs={"reconnect_grace_s": 20.0})
    node = str(new_node_id())
    async with running_controller(settings) as rt, session_channel(rt, node) as ch:
        agent = FakeAgent(ch, node_id=node)
        try:
            await agent.connect(hardware=_hw())
            agent.start_heartbeats(HB_S)
            job_id, start = await _running(agent, rt)
            await _drop(rt, agent)
        finally:
            await agent.close()
    # controller is down; the attempt must not have been ended by the shutdown
    async with running_controller(settings) as rt2:
        rows = await _sql(
            rt2,
            "SELECT state, detail FROM attempt WHERE job_id = :j ORDER BY fencing_token",
            j=job_id,
        )
        assert [r.state for r in rows] == ["running"], rows
        async with session_channel(rt2, node) as ch2:
            b = await _back(ch2, node, 3, start)
            try:
                await asyncio.sleep(3.0)
                rows = await _attempts(rt2, job_id)
                assert [(r.state, r.fencing_token) for r in rows] == [
                    ("running", start.fencing_token)
                ]
                assert _cancels(b) == []
                done = _status(b, start, common_pb2.ATTEMPT_STATE_DONE, exit_code=0)
                await b.write(done)
                await _until(lambda: _acked(b, done.message_id))
                assert (await _job(rt2, job_id))["state"] == "succeeded"
            finally:
                await b.close()


async def test_controller_restart_during_the_grace_a_dead_node_is_swept_after_reconnect_grace(
    pg_settings: DatabaseSettings,
) -> None:
    settings = _settings(pg_settings, 600.0, jobs={"reconnect_grace_s": 2.0})
    node = str(new_node_id())
    async with running_controller(settings) as rt, session_channel(rt, node) as ch:
        agent = FakeAgent(ch, node_id=node)
        try:
            await agent.connect(hardware=_hw())
            agent.start_heartbeats(HB_S)
            job_id, _start = await _running(agent, rt)
            await _drop(rt, agent)
        finally:
            await agent.close()
    async with running_controller(settings) as rt2:
        # the (forgotten) 600 s grace must not keep a dead node's attempt
        await _until(lambda: _lost(rt2, job_id), timeout_s=15.0)
        first = (await _attempts(rt2, job_id))[0]
        assert first.failure_type == "node_lost"


# ------------------------------------------------------------- cancel / timeout


async def test_cancel_during_the_grace_reaches_the_node_when_it_returns(
    pg_settings: DatabaseSettings,
) -> None:
    async with _rt(pg_settings, 30.0) as rt:
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            back: FakeAgent | None = None
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, start = await _running(agent, rt)
                await _drop(rt, agent)
                async with api_client(rt, token=token_of(rt)) as client:
                    got = await client.post(f"/v1/jobs/{job_id}/cancel")
                    assert got.status_code == 202, got.text
                assert (await _job(rt, job_id))["state"] not in (
                    "failed",
                    "succeeded",
                )
                back = await _back(ch, node, 2, start)
                await _until(lambda: bool(_cancels(back)))
                (c,) = _cancels(back)
                assert c.attempt_id == start.attempt_id
                assert c.fencing_token == start.fencing_token and not c.release
                await back.write(
                    _status(
                        back,
                        start,
                        common_pb2.ATTEMPT_STATE_FAILED,
                        failure=common_pb2.Failure(
                            failure_type=common_pb2.FAILURE_TYPE_CANCELLED,
                            message="cancelled",
                        ),
                    )
                )
                await _until(lambda: _job_is(rt, job_id, "cancelled"), timeout_s=10.0)
            finally:
                await agent.close()
                if back is not None:
                    await back.close()


async def _job_is(rt: ControllerRuntime, job_id: str, state: str) -> bool:
    return str((await _job(rt, job_id))["state"]) == state


async def test_cancel_during_the_grace_node_never_returns_job_ends_cancelled(
    pg_settings: DatabaseSettings,
) -> None:
    async with _rt(pg_settings, 2.0) as rt:
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, _start = await _running(agent, rt)
                await _drop(rt, agent)
                async with api_client(rt, token=token_of(rt)) as client:
                    assert (
                        await client.post(f"/v1/jobs/{job_id}/cancel")
                    ).status_code == 202
                # a cancelled job must never be re-run: it ends cancelled
                await _until(lambda: _job_is(rt, job_id, "cancelled"), timeout_s=15.0)
                rows = await _attempts(rt, job_id)
                assert len(rows) == 1, rows  # no retry attempt was created
            finally:
                await agent.close()


async def test_job_timeout_during_the_grace_the_backstop_still_fires(
    pg_settings: DatabaseSettings,
) -> None:
    async with _rt(pg_settings, 120.0, jobs={"backstop_grace_s": 1.0}) as rt:
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            back: FakeAgent | None = None
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, start = await _running(agent, rt, {"limits": {"timeout": "3s"}})
                await _drop(rt, agent)
                t0 = time.monotonic()
                await _until(lambda: _attempts_in(rt, job_id, "failed"), timeout_s=30.0)
                assert time.monotonic() - t0 < 30.0  # not waiting for the 120 s
                row = (await _attempts(rt, job_id))[0]
                assert (row.failure_type, row.detail) == (
                    "deadline_exceeded",
                    "controller_backstop",
                )
                # the stale hold must not resurrect or double-end anything
                back = await _back(ch, node, 2, start)
                await _until(lambda: bool(_cancels(back)))
                assert _cancels(back)[0].release
                assert rt.dispatcher is not None
                assert rt.dispatcher.stats.attempts_resumed == 0
            finally:
                await agent.close()
                if back is not None:
                    await back.close()


async def _attempts_in(rt: ControllerRuntime, job_id: str, state: str) -> bool:
    return (await _attempts(rt, job_id))[0].state == state


# ------------------------------------------------------------- revoke / 2nd loss


async def test_revoke_during_the_grace_ends_the_attempt_at_once(
    pg_settings: DatabaseSettings,
) -> None:
    async with _rt(pg_settings, 120.0) as rt:
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            back: FakeAgent | None = None
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, start = await _running(agent, rt)
                await _drop(rt, agent)
                t0 = time.monotonic()
                async with api_client(rt, token=token_of(rt)) as client:
                    got = await client.post(f"/v1/nodes/{node}/revoke")
                    assert got.status_code == 200, got.text
                await _until(lambda: _lost(rt, job_id), timeout_s=10.0)
                assert time.monotonic() - t0 < 10.0
                first = (await _attempts(rt, job_id))[0]
                assert first.failure_type == "node_lost"
                assert rt.dispatcher is not None
                assert start.attempt_id not in rt.dispatcher._held
            finally:
                await agent.close()
                if back is not None:
                    await back.close()


async def test_second_loss_after_a_resume_gets_a_fresh_grace(
    pg_settings: DatabaseSettings,
) -> None:
    async with _rt(pg_settings, 3.0) as rt:
        assert rt.dispatcher is not None
        d = rt.dispatcher
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            b: FakeAgent | None = None
            c: FakeAgent | None = None
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, start = await _running(agent, rt)
                await _drop(rt, agent)
                b = await _back(ch, node, 2, start)
                await _until(lambda: d.stats.attempts_resumed == 1)
                await asyncio.sleep(1.0)  # alive again for a while
                await _drop(rt, b, held=2)  # second loss
                assert (await _attempts(rt, job_id))[0].state == "running"
                c = await _back(ch, node, 3, start)
                await _until(lambda: d.stats.attempts_resumed == 2)
                await asyncio.sleep(4.0)
                rows = await _attempts(rt, job_id)
                assert [(r.state, r.fencing_token) for r in rows] == [
                    ("running", start.fencing_token)
                ]
                assert d.stats.attempts_lost == 0 and _cancels(c) == []
                await _drop(rt, c, held=3)  # third, and nobody comes back
                await _until(lambda: _lost(rt, job_id), timeout_s=15.0)
            finally:
                await agent.close()
                for x in (b, c):
                    if x is not None:
                        await x.close()


async def test_second_loss_before_the_reconcile_does_not_extend_the_first_grace(
    pg_settings: DatabaseSettings,
) -> None:
    async with _rt(pg_settings, 4.0) as rt:
        assert rt.dispatcher is not None
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            b: FakeAgent | None = None
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, start = await _running(agent, rt)
                await _drop(rt, agent)
                until = rt.dispatcher._held[start.attempt_id].until
                # reconnect but never Reconcile, then go silent again
                b = FakeAgent(ch, node_id=node, epoch=2)
                await b.connect(hardware=_hw())
                await asyncio.sleep(0.3)
                await b.abort()
                await asyncio.sleep(1.5)
                held = rt.dispatcher._held.get(start.attempt_id)
                assert held is None or held.until == until  # never extended
                await _until(lambda: _lost(rt, job_id), timeout_s=12.0)
            finally:
                await agent.close()
                if b is not None:
                    await b.close()


# ------------------------------------------------------------- API view


async def test_api_view_during_the_grace(pg_settings: DatabaseSettings) -> None:
    """The job shows running, the node offline, and nothing claims a failure."""
    async with _rt(pg_settings, 30.0) as rt:
        node = str(new_node_id())
        async with session_channel(rt, node) as ch:
            agent = FakeAgent(ch, node_id=node)
            try:
                await agent.connect(hardware=_hw())
                agent.start_heartbeats(HB_S)
                job_id, _start = await _running(agent, rt)
                await _drop(rt, agent)
                body = await _job(rt, job_id)
                assert body["state"] == "running", body
                async with api_client(rt, token=token_of(rt)) as client:
                    n = await client.get(f"/v1/nodes/{node}")
                    assert n.status_code == 200, n.text
                    assert n.json()["state"] == "offline", n.json()
                    listing = await client.get(f"/v1/jobs/{job_id}/attempts")
                    if listing.status_code == 200:
                        txt = listing.text
                        assert "lost" not in txt, txt
            finally:
                await agent.close()


# ------------------------------------------------------------- split lanes


async def _lane_job(rt: ControllerRuntime, tmp: Any, stop: int = 20) -> str:
    root = tmp / "p"
    root.mkdir(exist_ok=True)
    (root / "main.py").write_bytes(b"print(1)\n")
    built = build_tree(root)
    assert rt.blobs is not None
    rt.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        rt.blobs.cas.put_file(path)
    async with api_client(rt, token=token_of(rt)) as http:
        got = await http.post(
            "/v1/jobs",
            json={
                "manifest": {
                    "name": "grace-lane",
                    "entrypoint": "python main.py {items_file} {out}",
                    "split": {
                        "over": {"range": {"stop": stop}},
                        "chunk": {"items": 2},
                        "speculative": False,
                    },
                },
                "tree_id": str(built.tree.tree_id),
                "placement": {},
            },
        )
        assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


def _leases_sql() -> str:
    return (
        "SELECT item_id, seq, holder, state FROM work_lease "
        "WHERE state = 'live' ORDER BY item_id, seq"
    )


async def test_held_lane_leases_outlive_the_visibility_timeout_and_are_not_re_leased(
    pg_settings: DatabaseSettings, tmp_path: Any
) -> None:
    # visibility 5 s (the minimum), grace 14 s: the lease would expire at 5 s
    async with _rt(pg_settings, 14.0, split={"visibility_timeout_s": 5.0}) as rt:
        assert rt.dispatcher is not None
        d = rt.dispatcher
        async with NodeChannels(f"127.0.0.1:{rt.grpc_port}") as chs:
            a = FakeAgent(chs)
            await a.connect(hardware=_hw(True), max_cpu_jobs=2)
            a.start_heartbeats(HB_S)
            lane_a = FakeLane(a, rt, speed_s=0.0, hold_results=True)
            lane_a.start()
            b = FakeAgent(chs)
            await b.connect(hardware=_hw(True), max_cpu_jobs=2)
            b.start_heartbeats(HB_S)
            lane_b = FakeLane(b, rt, speed_s=0.0, hold_results=True)
            back: FakeAgent | None = None
            try:
                job_id = await _lane_job(rt, tmp_path)
                await _until(lambda: bool(lane_a.ledger))
                start = next(
                    m.start_attempt
                    for m in a.messages("start_attempt")
                    if m.start_attempt.HasField("lane")
                )
                mine = list(lane_a.ledger)
                await lane_a.close()
                await _drop(rt, a)
                # only now let B pull; B must never get A's chunk during the hold
                lane_b.start()
                await asyncio.sleep(9.0)  # > 5 s visibility, < 14 s grace
                live = await _sql(rt, _leases_sql())
                held_by_a = [r for r in live if r.holder == start.attempt_id]
                assert held_by_a, "A's lease went back to the queue"
                assert (held_by_a[0].seq) == mine[0][1]
                b_chunks = {c for c, _s in lane_b.ledger}
                assert mine[0][0] not in b_chunks, "double execution: chunk re-leased"
                # reconnect with the own token: still held after the old deadline
                back = await _back(chs, a.node_id, 2, start, split=True)
                await _until(lambda: d.stats.attempts_resumed == 1)
                live = await _sql(rt, _leases_sql())
                assert any(
                    r.holder == start.attempt_id and r.seq == mine[0][1] for r in live
                )
                assert _cancels(back) == []
                assert (await _attempts(rt, job_id))[0].state == "running"
            finally:
                await lane_a.close()
                await lane_b.close()
                for x in (a, b, back):
                    if x is not None:
                        with contextlib.suppress(Exception):
                            await x.close()


async def test_lane_after_the_grace_chunk_is_re_leased_and_the_old_token_is_fenced(
    pg_settings: DatabaseSettings, tmp_path: Any
) -> None:
    async with _rt(pg_settings, 2.0, split={"visibility_timeout_s": 5.0}) as rt:  # noqa: SIM117
        async with NodeChannels(f"127.0.0.1:{rt.grpc_port}") as chs:
            a = FakeAgent(chs)
            await a.connect(hardware=_hw(True), max_cpu_jobs=2)
            a.start_heartbeats(HB_S)
            lane_a = FakeLane(a, rt, speed_s=0.0, hold_results=True)
            lane_a.start()
            b = FakeAgent(chs)
            await b.connect(hardware=_hw(True), max_cpu_jobs=2)
            b.start_heartbeats(HB_S)
            lane_b = FakeLane(b, rt, speed_s=0.0)
            back: FakeAgent | None = None
            try:
                job_id = await _lane_job(rt, tmp_path, stop=6)
                await _until(lambda: bool(lane_a.ledger))
                start = next(
                    m.start_attempt
                    for m in a.messages("start_attempt")
                    if m.start_attempt.HasField("lane")
                )
                chunk_idx, seq = lane_a.ledger[0]
                await lane_a.close()
                await _drop(rt, a)
                lane_b.start()

                # grace (2 s) ends -> A's lane lost, B finishes every chunk
                async def done() -> bool:
                    return (await _job(rt, job_id))["state"] == "succeeded"

                await _until(done, timeout_s=40.0)
                assert chunk_idx in {c for c, _ in lane_b.ledger}
                # A returns late and replays its chunk result with the old token
                back = await _back(chs, a.node_id, 2, start, split=True)
                await _until(lambda: bool(_cancels(back)))
                assert _cancels(back)[0].release
                await back.write(
                    _msg(
                        back,
                        chunk_result=agent_pb2.ChunkResult(
                            job_id=start.job_id,
                            attempt_id=start.attempt_id,
                            fencing_token=start.fencing_token,
                            chunk_index=chunk_idx,
                            item_id=0,
                            lease_seq=seq,
                            outcome=agent_pb2.ChunkResult.OUTCOME_DONE,
                            result_digest=str(sha256_bytes(b"evil")),
                            result_bytes=4,
                        ),
                    )
                )
                await asyncio.sleep(1.0)
                assert (await _job(rt, job_id))["state"] == "succeeded"
            finally:
                await lane_a.close()
                await lane_b.close()
                for x in (a, b, back):
                    if x is not None:
                        with contextlib.suppress(Exception):
                            await x.close()
