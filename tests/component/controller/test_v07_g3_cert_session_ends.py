"""V0.7 G3 fix round 1 (D155 T-1, T-3): a live session never outlives the
certificate it was admitted with. Real PostgreSQL, the real
``ControllerRuntime`` in ``mtls`` mode, real gRPC/TLS clients.

* **T-1** - a re-enrolment supersedes the node's old certificate (its serial
  joins ``revoked_cert`` as ``superseded``, written by the enrolment
  transaction): the old laptop's LIVE stream is cut by the periodic revocation
  sync - no explicit sync call - within the poll interval (the same path and
  bound as a CLI revoke), ``PERMISSION_DENIED`` + ``meshforge-reason:
  cert_superseded``; the session row ends ``cert_superseded``, the node goes
  OFFLINE at once (node-loss path, no linger); a bystander is untouched.
* **T-3** - a stream whose certificate reaches ``not_after`` is cut at that
  moment: ``UNAUTHENTICATED`` + ``cert_expired``, session row ``cert_expired``,
  node OFFLINE; the expiry timer of a session that closed earlier is cancelled.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_forged_certs import Forged, forge, grpc_credentials
from mf_session_client import FakeAgent

from meshforge.common import pki
from meshforge.controller.revocation import supersede
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


def _channel(rt: ControllerRuntime, forged: Forged) -> grpc.aio.Channel:
    return grpc.aio.secure_channel(
        f"127.0.0.1:{rt.grpc_port}",
        grpc_credentials(forged),
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    )


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    from sqlalchemy import text

    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _eventually(
    pg: DatabaseSettings, statement: str, want: Any, **p: Any
) -> None:
    end = time.monotonic() + 10
    rows: list[Any] = []
    while time.monotonic() < end:
        rows = await _sql(pg, statement, **p)
        if rows == want:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{statement}: {rows} != {want}")


async def _trailer(agent: FakeAgent) -> str | None:
    metadata = await agent._call.trailing_metadata()
    return dict(metadata or ()).get("meshforge-reason")


async def test_superseded_cert_cuts_its_live_session_within_the_poll_interval(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    old, bystander = forge(), forge()
    async with _channel(runtime, old) as ch_old, _channel(runtime, bystander) as ch_b:
        agent = FakeAgent(ch_old, node_id=old.node_id)
        other = FakeAgent(ch_b, node_id=bystander.node_id)
        await agent.connect()
        await other.connect()
        agent.start_heartbeats(0.1)
        other.start_heartbeats(0.1)
        try:
            engine = create_engine(pg_settings)
            try:
                async with create_session_factory(engine)() as db, db.begin():
                    await supersede(
                        db,
                        node_id=old.node_id,
                        serial=old.serial,
                        not_after=old.cert.not_valid_after_utc,
                    )
            finally:
                await engine.dispose()
            started = time.monotonic()
            # Only the periodic sync ([controller.revocation] poll_interval_s,
            # 1 s) can find it - as for a re-enrolment or a CLI revoke.
            await asyncio.wait_for(agent.ended.wait(), timeout=5)
            took = time.monotonic() - started
            assert took < 1.0 + 1.0, f"cut after {took:.2f} s"
            assert agent.end_code is grpc.StatusCode.PERMISSION_DENIED
            assert agent.end_details.startswith("cert_superseded")
            assert await _trailer(agent) == "cert_superseded"
            await asyncio.sleep(0.5)
            assert not other.ended.is_set(), "the bystander was cut too"
        finally:
            await agent.close()
            await other.close()
    await _eventually(
        pg_settings,
        "SELECT end_reason FROM node_session WHERE node_id = :n",
        [("cert_superseded",)],
        n=old.node_id,
    )
    await _eventually(
        pg_settings,
        "SELECT state FROM node WHERE node_id = :n",
        [("offline",)],
        n=old.node_id,
    )
    assert runtime.manager is not None
    assert not runtime.manager.has_session(old.node_id)  # OFFLINE now: no linger
    assert runtime.revocations is not None and runtime.revocations.cert_cuts == 1


async def test_a_session_is_cut_at_its_certificates_not_after(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    # pki's shortest lifetime is one minute: issue it 54 s "ago" -> 6 s left
    short = forge(
        now=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=54),
        lifetime=dt.timedelta(minutes=1),
    )
    async with _channel(runtime, short) as channel:
        agent = FakeAgent(channel, node_id=short.node_id)
        await agent.connect()
        agent.start_heartbeats(0.2)
        try:
            remaining = (
                short.cert.not_valid_after_utc - dt.datetime.now(dt.UTC)
            ).total_seconds()
            assert remaining > 1, "test setup too slow"
            await asyncio.sleep(remaining - 1)
            assert not agent.ended.is_set(), "cut before not_after"
            await asyncio.wait_for(agent.ended.wait(), timeout=3)
            late = (
                dt.datetime.now(dt.UTC) - short.cert.not_valid_after_utc
            ).total_seconds()
            assert late < 1.5, f"cut {late:.2f} s after not_after"
            assert agent.end_code is grpc.StatusCode.UNAUTHENTICATED
            assert agent.end_details.startswith("cert_expired")
            assert await _trailer(agent) == "cert_expired"
        finally:
            await agent.close()
    await _eventually(
        pg_settings,
        "SELECT end_reason FROM node_session WHERE node_id = :n",
        [("cert_expired",)],
        n=short.node_id,
    )
    await _eventually(
        pg_settings,
        "SELECT state FROM node WHERE node_id = :n",
        [("offline",)],
        n=short.node_id,
    )


async def test_a_closed_session_cancels_its_certificate_expiry_timer(
    runtime: ControllerRuntime,
) -> None:
    node = forge(lifetime=dt.timedelta(hours=1))
    assert runtime.manager is not None
    async with _channel(runtime, node) as channel:
        agent = FakeAgent(channel, node_id=node.node_id)
        await agent.connect()
        (live,) = runtime.manager.snapshot()
        assert live.cert_serial == node.serial
        timer = live.cert_expiry
        assert timer is not None
        delay = timer.when() - asyncio.get_running_loop().time()
        assert 3500 < delay <= 3600 + 5
        await agent.close()
        end = time.monotonic() + 5
        while not live.ended and time.monotonic() < end:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    assert live.ended and live.cert_expiry is None
    assert timer.cancelled()
