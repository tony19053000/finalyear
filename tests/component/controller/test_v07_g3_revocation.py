"""V0.7 G3 deliverable 6 (roadmap 0.7.7a, D138): node revocation on real
PostgreSQL, the real ``ControllerRuntime`` in ``mtls`` mode and real gRPC/TLS
clients.

* ``POST /v1/nodes/{id}/revoke`` cuts the node's live stream with
  ``PERMISSION_DENIED`` / ``node_revoked`` - measured from the request to the
  agent seeing the status, asserted under one (production) heartbeat; the DB
  flag, the ``revoked_cert`` row and the ``node_revoked`` audit row exist;
  revoking again changes nothing; a reconnect with the same certificate is
  refused by the interceptor (``cert_revoked``) and a fetch token minted for
  the node no longer works with its certificate on the HTTPS plane;
* every other live agent gets ``RevocationUpdate`` with the serial, a new
  session's ``HelloAck`` carries the whole set;
* a revocation written by the controller CLI (another process: straight into
  the database) is enforced by the periodic sync within its interval;
* who may revoke: the fabric token (401 without), 404 unknown, 400 not an id;
* no un-revoke: a plain invite cannot re-enrol a revoked node, an ``invite
  --replace`` code can (new serial; the old one stays refused); a re-keyed
  node's previous certificate joins the set as ``superseded``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import grpc
import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_fabric import NodeChannels, client_ssl_context, grpc_channel, node_material
from mf_jobs_harness import api_client, token_of
from mf_session_client import FakeAgent, make_hello

from meshforge.agent import enrollment as agent_enrollment
from meshforge.agent.enrollment import JoinError
from meshforge.agent.identity import load_or_create
from meshforge.common import pki
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.app import _revoke
from meshforge.controller.enrollment.codes import issue_code
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

PRODUCTION_HEARTBEAT_S = LivenessSettings().heartbeat_interval_s


def _target(rt: ControllerRuntime) -> str:
    return f"127.0.0.1:{rt.grpc_port}"


def _serial(node_id: str) -> str:
    return format(pki.cert_from_pem(node_material(node_id)[1]).serial_number, "x")


async def _sql(
    pg: DatabaseSettings, statement: str, params: dict[str, Any] | None = None
) -> list[Any]:
    from sqlalchemy import text

    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _until(check: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


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


_FABRIC = object()
"""``_revoke_rest``'s default credential: the runtime's fabric token."""


async def _revoke_rest(
    runtime: ControllerRuntime, node_id: str, token: object = _FABRIC
) -> httpx.Response:
    bearer = token_of(runtime) if token is _FABRIC else token
    assert bearer is None or isinstance(bearer, str)
    async with api_client(runtime, token=bearer) as http:
        return await http.post(f"/v1/nodes/{node_id}/revoke")


# ------------------------------------------------------------- the REST path


async def test_revoke_cuts_the_live_session_within_one_heartbeat(
    runtime: ControllerRuntime, registry: Registry, pg_settings: DatabaseSettings
) -> None:
    async with NodeChannels(_target(runtime)) as pool:
        victim = FakeAgent(pool)
        await victim.connect()
        victim.start_heartbeats(0.2)
        serial = _serial(victim.node_id)
        await _until(lambda: runtime.manager is not None and len(runtime.manager) == 1)
        started = time.monotonic()
        response = await _revoke_rest(runtime, victim.node_id)
        await victim.wait_ended(5.0)
        elapsed = time.monotonic() - started
        victim.stop_heartbeats()
        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {
            "node_id": victim.node_id,
            "state": "revoked",
            "serial": serial,
            "already_revoked": False,
            "previous_state": "online",
            "session_killed": True,
        }
        assert victim.end_code is grpc.StatusCode.PERMISSION_DENIED
        assert victim.end_details.startswith("node_revoked")
        assert "fabric invite --replace" in victim.end_details
        # Criterion 1: "within one heartbeat" - by construction, measured here.
        assert elapsed < PRODUCTION_HEARTBEAT_S, elapsed

        record = await registry.get_node(victim.node_id)
        assert record is not None and record.state is NodeState.REVOKED
        rows = await _sql(
            pg_settings,
            "SELECT reason, node_id FROM revoked_cert WHERE serial = :s",
            {"s": serial},
        )
        assert rows == [("revoked", victim.node_id)]
        audit = await _sql(
            pg_settings,
            "SELECT actor, details->>'serial' FROM audit_event "
            "WHERE action = 'node_revoked' AND node_id = :n",
            {"n": victim.node_id},
        )
        assert audit == [("rest:fabric-token", serial)]
        session = await _sql(
            pg_settings,
            "SELECT end_reason FROM node_session WHERE session_id = :s",
            {"s": victim.session_id},
        )
        assert session in ([("revoked",)], [(None,)])  # written on the chain

        again = await _revoke_rest(runtime, victim.node_id)
        assert again.status_code == 200
        assert again.json()["already_revoked"] is True
        assert (
            len(
                await _sql(
                    pg_settings,
                    "SELECT 1 FROM audit_event WHERE action = 'node_revoked' "
                    "AND node_id = :n",
                    {"n": victim.node_id},
                )
            )
            == 1
        )

    # A reconnect with the same certificate: refused before any Hello logic.
    async with grpc_channel(_target(runtime), victim.node_id) as channel:
        code, trailing = await _hello_status(channel, victim.node_id, 7)
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert trailing.get("meshforge-reason") == "cert_revoked"
    record = await registry.get_node(victim.node_id)
    assert record is not None and record.session_epoch == 1  # nothing admitted

    # Revoked is sticky: a late session write cannot turn it back.
    assert record.last_session_id is not None
    assert not await registry.set_node_state(
        victim.node_id, NodeState.ONLINE, session_id=record.last_session_id
    )
    again_record = await registry.get_node(victim.node_id)
    assert again_record is not None and again_record.state is NodeState.REVOKED


async def test_the_https_plane_refuses_the_revoked_certificate(
    runtime: ControllerRuntime,
) -> None:
    async with NodeChannels(_target(runtime)) as pool:
        victim = FakeAgent(pool)
        await victim.connect()
        blob = b"revocation-blob" * 64
        digest = sha256_bytes(blob)
        async with api_client(runtime, token=token_of(runtime)) as http:
            assert (
                await http.put(f"/v1/blobs/{digest.hex}", content=blob)
            ).status_code == 201
        assert runtime.fetch_tokens is not None
        token = runtime.fetch_tokens.mint(
            str(new_attempt_id()), [digest], ttl_s=60, node_id=victim.node_id
        ).get_secret_value()
        url = f"https://127.0.0.1:{runtime.http_port}/v1/blobs/{digest.hex}"
        headers = {"authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            verify=client_ssl_context(node_id=victim.node_id), timeout=10
        ) as http:
            assert (await http.get(url, headers=headers)).status_code == 200
        assert (await _revoke_rest(runtime, victim.node_id)).status_code == 200
        async with httpx.AsyncClient(
            verify=client_ssl_context(node_id=victim.node_id), timeout=10
        ) as http:
            refused = await http.get(url, headers=headers)
        assert refused.status_code == 401, refused.text
        await victim.wait_ended(5.0)


async def test_every_agent_learns_the_serial_and_new_sessions_get_the_set(
    runtime: ControllerRuntime,
) -> None:
    async with NodeChannels(_target(runtime)) as pool:
        bystander = FakeAgent(pool)
        hello = await bystander.connect()
        victim = FakeAgent(pool)
        await victim.connect()
        serial = _serial(victim.node_id)
        assert serial not in hello.revoked_serials
        assert (await _revoke_rest(runtime, victim.node_id)).status_code == 200
        await _until(lambda: bool(bystander.messages("revocation_update")), 5.0)
        (update,) = bystander.messages("revocation_update")
        assert serial in update.revocation_update.serials
        latecomer = FakeAgent(pool)
        ack = await latecomer.connect()
        assert serial in ack.revoked_serials
        await latecomer.close()
        await bystander.close()


async def test_a_cli_revocation_is_enforced_by_the_periodic_sync(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    interval = runtime.settings.controller.revocation.poll_interval_s
    async with NodeChannels(_target(runtime)) as pool:
        victim = FakeAgent(pool)
        await victim.connect()
        victim.start_heartbeats(0.2)
        started = time.monotonic()
        outcome = await _revoke(runtime.settings, victim.node_id)  # the CLI's path
        assert outcome["already_revoked"] is False
        assert outcome["serial"] == _serial(victim.node_id)
        await victim.wait_ended(interval + 3.0)
        victim.stop_heartbeats()
        assert victim.end_code is grpc.StatusCode.PERMISSION_DENIED
        assert time.monotonic() - started < interval + 2.0
    record = await registry.get_node(victim.node_id)
    assert record is not None and record.state is NodeState.REVOKED


async def test_who_may_revoke(runtime: ControllerRuntime) -> None:
    node = str(new_node_id())
    assert (await _revoke_rest(runtime, node, token=None)).status_code == 401
    assert (await _revoke_rest(runtime, node, token="wrong")).status_code == 401
    assert (await _revoke_rest(runtime, node)).status_code == 404
    assert (await _revoke_rest(runtime, "not-a-node")).status_code == 400


# ------------------------------------------------------------- no un-revoke


async def test_only_a_replace_code_brings_a_revoked_node_back(
    runtime: ControllerRuntime,
    registry: Registry,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    assert runtime.fabric is not None and runtime.enroll_port > 0
    engine = create_engine(pg_settings)
    invites = InviteStore(engine)

    async def code(replace: str | None = None) -> str:
        new = await invites.create(ttl_s=900, actor="test", replace_node_id=replace)
        assert runtime.fabric is not None
        return issue_code(
            new, fabric=runtime.fabric, host="127.0.0.1", port=runtime.enroll_port
        ).code

    try:
        identity = load_or_create(tmp_path / "laptop" / "identity")
        first = await asyncio.to_thread(agent_enrollment.join, identity, await code())
        old_serial = format(first.enrollment.node_cert.serial_number, "x")
        assert (await _revoke_rest(runtime, identity.node_id)).status_code == 200
        with pytest.raises(JoinError) as refused:
            await asyncio.to_thread(
                agent_enrollment.join, identity, await code(), force=True
            )
        assert refused.value.controller_reason == "node_revoked"
        back = await asyncio.to_thread(
            agent_enrollment.join,
            identity,
            await code(replace=identity.node_id),
            force=True,
        )
        new_serial = format(back.enrollment.node_cert.serial_number, "x")
        assert new_serial != old_serial
        record = await registry.get_node(identity.node_id)
        assert record is not None and record.state is NodeState.ENROLLING
        assert record.cert_serial == new_serial
        assert (
            await _sql(
                pg_settings,
                "SELECT reason FROM revoked_cert WHERE serial = :s",
                {"s": old_serial},
            )
        ) == [("revoked",)]
        # A plain re-key of the (now enrolled again) node: the replaced
        # certificate is listed as superseded and the sync picks it up.
        third = await asyncio.to_thread(
            agent_enrollment.join,
            identity,
            await code(),
            force=True,
        )
        assert (
            await _sql(
                pg_settings,
                "SELECT reason FROM revoked_cert WHERE serial = :s",
                {"s": new_serial},
            )
        ) == [("superseded",)]
        assert runtime.revocations is not None
        await _until(lambda: new_serial in runtime.revocations.serials, 5.0)  # type: ignore[union-attr]
        assert format(third.enrollment.node_cert.serial_number, "x") not in (
            runtime.revocations.serials
        )
    finally:
        await engine.dispose()
