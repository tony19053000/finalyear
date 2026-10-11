"""V0.7 G4 item 0 (D156) on real PostgreSQL and the real ``ControllerRuntime``.

* (a) ``POST /v1/nodes/{id}/revoke`` answers 200 ``enforcement: deferred``
  (never a 500) when the revocation is committed but the immediate sync fails;
  the periodic sync cuts the live session afterwards and admission refuses a
  reconnect from the database row meanwhile;
* (b) a reconnect with a **superseded** serial is refused ``cert_superseded``
  (``PERMISSION_DENIED``) by the interceptor, a revoked one ``cert_revoked``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_fabric import NodeChannels, grpc_channel, node_material
from mf_jobs_harness import api_client, token_of
from mf_session_client import FakeAgent, make_hello

from meshforge.common import pki
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.revocation import supersede
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


def _target(rt: ControllerRuntime) -> str:
    return f"127.0.0.1:{rt.grpc_port}"


async def _hello_status(
    channel: grpc.aio.Channel, node_id: str, epoch: int
) -> tuple[grpc.StatusCode | None, dict[str, str]]:
    call = agent_pb2_grpc.AgentControlStub(channel).Session(timeout=10)
    try:
        await call.write(
            agent_pb2.AgentMessage(
                message_id=new_command_id(), hello=make_hello(node_id, epoch)
            )
        )
        reply = await call.read()
        if reply is not grpc.aio.EOF and reply.hello_ack.accepted:
            return grpc.StatusCode.OK, {}
        code, trailing = await call.code(), await call.trailing_metadata()
    except grpc.aio.AioRpcError as exc:
        code, trailing = exc.code(), exc.trailing_metadata()
    finally:
        call.cancel()
    return code, {k: v for k, v in (trailing or ()) if isinstance(v, str)}


async def test_a_committed_revoke_whose_sync_fails_is_200_deferred(
    runtime: ControllerRuntime,
) -> None:
    revocations = runtime.revocations
    assert revocations is not None
    async with NodeChannels(_target(runtime)) as pool:
        victim = FakeAgent(pool)
        await victim.connect()
        victim.start_heartbeats(0.2)
        real_snapshot = revocations._store.snapshot
        calls = {"n": 0}

        async def flaky() -> Any:
            calls["n"] += 1
            if calls["n"] == 1:  # the REST path's immediate sync
                from meshforge.controller.revocation import RevocationError

                raise RevocationError("down", details={"cause": "OSError"})
            return await real_snapshot()

        revocations._store.snapshot = flaky  # type: ignore[method-assign]
        try:
            async with api_client(runtime, token=token_of(runtime)) as http:
                response = await http.post(f"/v1/nodes/{victim.node_id}/revoke")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["enforcement"] == "deferred"
            assert body["already_revoked"] is False
            assert revocations.deferred_enforcements >= 1
            # The periodic sync (poll interval) cuts the live stream anyway.
            started = time.monotonic()
            await victim.wait_ended(10.0)
            assert time.monotonic() - started < 10.0
        finally:
            revocations._store.snapshot = real_snapshot  # type: ignore[method-assign]


async def test_a_superseded_serial_reconnects_as_cert_superseded(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    async with NodeChannels(_target(runtime)) as pool:
        agent = FakeAgent(pool)
        await agent.connect()
        node_id = agent.node_id
        await agent.close()
    cert = pki.cert_from_pem(node_material(node_id)[1])
    engine = create_engine(pg_settings)
    try:
        async with create_session_factory(engine)() as db, db.begin():
            await supersede(
                db,
                node_id=node_id,
                serial=format(cert.serial_number, "x"),
                not_after=cert.not_valid_after_utc,
            )
    finally:
        await engine.dispose()
    assert runtime.revocations is not None
    await runtime.revocations.sync()
    async with grpc_channel(_target(runtime), node_id) as channel:
        code, trailing = await _hello_status(channel, node_id, 9)
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert trailing.get("meshforge-reason") == "cert_superseded"
    await asyncio.sleep(0)
