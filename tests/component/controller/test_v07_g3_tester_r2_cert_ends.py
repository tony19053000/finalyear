"""V0.7 G3 tester round 2 (D155 T-1, T-3): the certificate-bound session ends
under races and clock edges. Real PostgreSQL, real ControllerRuntime in mtls
mode, real gRPC/TLS streams.

* a lingering (stream-lost, SUSPECT) session of a superseded serial goes
  OFFLINE at once, not at its lease edge;
* the expiry timer of a replaced session never cuts its replacement;
* a certificate with milliseconds left never leaves a zombie session;
* a superseded cut racing a graceful close / reconnect storm leaves no live
  session of the superseded serial and no unhandled error;
* the old laptop, once cut, is refused again at every Hello.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import time
from typing import Any

import grpc
import pytest
from mf_controller_harness import make_settings, tolerant_liveness
from mf_forged_certs import Forged, forge, grpc_credentials
from mf_session_client import FakeAgent, make_hello

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common import pki
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.revocation import supersede
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    # Silence for 10 s before OFFLINE: only the cut can explain a faster OFFLINE.
    return make_settings(pg_settings, **tolerant_liveness())


def _channel(rt: ControllerRuntime, forged: Forged) -> grpc.aio.Channel:
    return grpc.aio.secure_channel(
        f"127.0.0.1:{rt.grpc_port}",
        grpc_credentials(forged),
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    )


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _state(pg: DatabaseSettings, node_id: str) -> str | None:
    rows = await _sql(pg, "SELECT state FROM node WHERE node_id = :n", n=node_id)
    return rows[0][0] if rows else None


async def _supersede(pg: DatabaseSettings, forged: Forged) -> None:
    engine = create_engine(pg)
    try:
        async with create_session_factory(engine)() as db, db.begin():
            await supersede(
                db,
                node_id=forged.node_id,
                serial=forged.serial,
                not_after=forged.cert.not_valid_after_utc,
            )
    finally:
        await engine.dispose()


async def _reason_of(agent: FakeAgent) -> str | None:
    md = await agent._call.trailing_metadata()
    return dict(md or ()).get("meshforge-reason")


async def _wait(predicate: Any, timeout_s: float, what: str) -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out: {what}")


async def _hello_once(
    rt: ControllerRuntime, forged: Forged
) -> tuple[grpc.StatusCode | None, str | None]:
    async with _channel(rt, forged) as channel:
        call = agent_pb2_grpc.AgentControlStub(channel).Session(timeout=10)
        try:
            await call.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    hello=make_hello(forged.node_id, 1),
                )
            )
            reply = await call.read()
            if (
                reply is not grpc.aio.EOF
                and reply.WhichOneof("payload") == "hello_ack"
                and reply.hello_ack.accepted
            ):
                return grpc.StatusCode.OK, None
            md = dict(await call.trailing_metadata() or ())
            return await call.code(), md.get("meshforge-reason")
        except grpc.aio.AioRpcError as exc:
            md = dict(exc.trailing_metadata() or ())
            return exc.code(), md.get("meshforge-reason")
        finally:
            call.cancel()


async def test_a_lingering_session_of_a_superseded_serial_goes_offline_now(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    node = forge()
    async with _channel(runtime, node) as channel:
        agent = FakeAgent(channel, node_id=node.node_id)
        await agent.connect()
        agent.start_heartbeats(0.2)
        await agent.abort()  # stream lost: SUSPECT + lingering, lease is 10 s
    assert runtime.manager is not None
    await _wait(lambda: _async(runtime.manager.has_session(node.node_id)), 5, "linger")
    assert await _state(pg_settings, node.node_id) == "suspect"
    started = time.monotonic()
    await _supersede(pg_settings, node)
    await _wait(
        lambda: _async(not runtime.manager.has_session(node.node_id)), 6, "linger gone"
    )
    took = time.monotonic() - started
    assert took < 4.0, f"linger survived {took:.1f} s after supersede"
    assert await _state(pg_settings, node.node_id) == "offline"
    assert runtime.revocations is not None and runtime.revocations.cert_cuts >= 1


async def _async(value: Any) -> Any:
    return value


async def test_the_cut_old_laptop_is_refused_again_at_every_hello(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    node = forge()
    async with _channel(runtime, node) as channel:
        agent = FakeAgent(channel, node_id=node.node_id)
        await agent.connect()
        agent.start_heartbeats(0.2)
        # Stands in for enrolment: the node now records another certificate.
        await _sql(
            pg_settings,
            "UPDATE node SET cert_serial = :s WHERE node_id = :n",
            s="abc123",
            n=node.node_id,
        )
        await _supersede(pg_settings, node)
        await asyncio.wait_for(agent.ended.wait(), timeout=5)
        assert await _reason_of(agent) == "cert_superseded"
        await agent.close()
    for _ in range(3):
        code, reason = await _hello_once(runtime, node)
        # Either plane's wording is fine (the interceptor sees the serial in the
        # set first - V0.7 G4 item 0, D156 b: as superseded); never admitted.
        assert (code, reason) in {
            (grpc.StatusCode.PERMISSION_DENIED, "cert_superseded"),
            (grpc.StatusCode.UNAUTHENTICATED, "cert_superseded"),
        }, (code, reason)
    assert runtime.manager is not None and not runtime.manager.has_session(node.node_id)


async def test_a_replaced_sessions_expiry_timer_never_cuts_its_replacement(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    """Session 1 holds a certificate with ~4 s left. The node is re-keyed
    (the record names certificate B) and re-Hellos with B (long life): session
    1 is replaced. When certificate A would have expired, session 2 is alive."""
    short = forge(
        now=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=56),
        lifetime=dt.timedelta(minutes=1),
    )
    long_ = forge(node_id=short.node_id, lifetime=dt.timedelta(hours=1))
    assert runtime.manager is not None
    async with _channel(runtime, short) as ch1, _channel(runtime, long_) as ch2:
        first = FakeAgent(ch1, node_id=short.node_id)
        await first.connect()
        first.start_heartbeats(0.2)
        (live1,) = runtime.manager.snapshot()
        assert live1.cert_expiry is not None
        await _sql(
            pg_settings,
            "UPDATE node SET cert_serial = :s, cert_not_after = :na WHERE node_id = :n",
            s=long_.serial,
            na=long_.cert.not_valid_after_utc,
            n=short.node_id,
        )
        second = FakeAgent(ch2, node_id=short.node_id, epoch=2)
        await second.connect()
        second.start_heartbeats(0.2)
        await asyncio.wait_for(first.ended.wait(), timeout=5)  # replaced
        assert live1.cert_expiry is None, "old timer still armed"
        remaining = (
            short.cert.not_valid_after_utc - dt.datetime.now(dt.UTC)
        ).total_seconds()
        await asyncio.sleep(max(0.0, remaining) + 2.0)  # well past A's not_after
        assert not second.ended.is_set(), (second.end_code, second.end_details)
        (live2,) = runtime.manager.snapshot()
        assert live2.cert_serial == long_.serial and live2.cert_expiry is not None
        assert await _state(pg_settings, short.node_id) == "online"
        await first.close()
        await second.close()


@pytest.mark.parametrize("margin_s", [0.05, 0.2, 0.5, 1.0])
async def test_a_certificate_with_milliseconds_left_never_leaves_a_zombie(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings, margin_s: float
) -> None:
    cert = forge(
        now=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=60 - margin_s),
        lifetime=dt.timedelta(minutes=1),
    )
    assert runtime.manager is not None
    connected = False
    async with _channel(runtime, cert) as channel:
        agent = FakeAgent(channel, node_id=cert.node_id)
        try:
            await asyncio.wait_for(agent.connect(), timeout=5)
            connected = True
            agent.start_heartbeats(0.1)
        except (AssertionError, grpc.aio.AioRpcError, TimeoutError):
            pass  # refused at the handshake / Hello: also correct
        if connected:
            await asyncio.wait_for(agent.ended.wait(), timeout=margin_s + 4)
            assert agent.end_code in (
                grpc.StatusCode.UNAUTHENTICATED,
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.CANCELLED,
            ), agent.end_code
        with contextlib.suppress(Exception):
            await agent.close()
    await _wait(
        lambda: _async(not runtime.manager.has_session(cert.node_id)),
        5,
        "no live session",
    )
    state = await _state(pg_settings, cert.node_id)
    for _ in range(40):  # the OFFLINE write follows the end by a moment
        if state in (None, "offline", "suspect"):
            break
        await asyncio.sleep(0.25)
        state = await _state(pg_settings, cert.node_id)
    assert state in (None, "offline", "suspect"), f"stuck {state} (zombie)"


async def test_supersede_racing_a_graceful_close_leaves_nothing_live(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    """Many nodes: half say Goodbye at the moment their serial is superseded.
    Every node ends OFFLINE, no session of a superseded serial stays live."""
    nodes = [forge() for _ in range(8)]
    assert runtime.manager is not None
    channels = [_channel(runtime, n) for n in nodes]
    agents = [
        FakeAgent(ch, node_id=n.node_id) for ch, n in zip(channels, nodes, strict=True)
    ]
    try:
        for a in agents:
            await a.connect()
            a.start_heartbeats(0.1)
        # V0.7 G6 (D161 load flake): a heartbeat *write* in flight when the
        # controller cuts the call makes grpc core end the client's call
        # INTERNAL ("execute_batch") instead of delivering the server's
        # PERMISSION_DENIED - a client-side artefact (the controller recorded
        # cert_superseded every time; asserted below). The cut agents stop
        # writing first so the status they read is the server's; liveness here
        # is tolerant, so no lease runs out meanwhile.
        for a in agents[1::2]:
            await a.finish_heartbeats()  # never cancels a write in flight
        await asyncio.gather(
            *[_supersede(pg_settings, n) for n in nodes],
            *[a.close(graceful=True) for a in agents[::2]],
        )
        for a in agents[1::2]:
            await asyncio.wait_for(a.ended.wait(), timeout=6)
            assert a.end_code is grpc.StatusCode.PERMISSION_DENIED
        for n in nodes:
            await _wait(
                lambda n=n: _async(not runtime.manager.has_session(n.node_id)),  # type: ignore[union-attr]
                6,
                f"{n.node_id} gone",
            )
        for n in nodes:
            assert await _state(pg_settings, n.node_id) == "offline"
        assert [s for s in runtime.manager.snapshot() if s.cert_serial] == []
        for a in agents[1::2]:
            rows = await _sql(
                pg_settings,
                "SELECT end_reason FROM node_session WHERE node_id = :n",
                n=a.node_id,
            )
            assert rows == [("cert_superseded",)], rows
    finally:
        for a in agents:
            with contextlib.suppress(Exception):
                await a.close()
        for ch in channels:
            await ch.close()


async def test_a_superseded_sessions_end_is_recorded_exactly_once(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    node = forge()
    async with _channel(runtime, node) as channel:
        agent = FakeAgent(channel, node_id=node.node_id)
        await agent.connect()
        agent.start_heartbeats(0.1)
        await _supersede(pg_settings, node)
        await asyncio.wait_for(agent.ended.wait(), timeout=5)
        await agent.close()
    # the periodic sync keeps running with the serial still in the set
    await asyncio.sleep(2.5)
    rows = await _sql(
        pg_settings,
        "SELECT end_reason FROM node_session WHERE node_id = :n",
        n=node.node_id,
    )
    assert rows == [("cert_superseded",)]
    assert runtime.revocations is not None and runtime.revocations.cert_cuts == 1
