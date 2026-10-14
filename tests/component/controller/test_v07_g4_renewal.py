"""V0.7 G4 deliverable 1 (roadmap 0.7.7b, D140): ``RenewCertificate`` on real
PostgreSQL, the real ``ControllerRuntime`` (mutual TLS) and real gRPC/TLS
clients.

* a renewal over the live session's certificate issues a **pending**
  certificate: the live session is not cut, the old serial is NOT superseded
  yet, an audit row exists;
* the node's first Hello with the new certificate activates it: the node row
  records it, the old serial joins the revocation set as ``superseded`` (sent
  to the other agents at once), ``cert_activated`` is audited, and the old
  certificate is refused (``cert_superseded``) from then on;
* refusals, each typed (status + ``meshforge-reason``) and issuing nothing: a
  CSR for another node, a CSR from another fabric, a second renewal inside the
  rate-limit window (``RESOURCE_EXHAUSTED`` + retry-after), an expired client
  certificate (no TLS handshake at all), a revoked node, a superseded
  certificate;
* renewal racing revocation: whichever commits first, no certificate of a
  revoked node is ever left unlisted;
* a key rotation: the CSR's new key is certified and becomes the node's
  ``public_key_fingerprint`` at activation;
* an unused pending certificate is superseded by the next renewal and revoked
  with the node.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import make_settings, tolerant_liveness
from mf_forged_certs import Forged, forge, grpc_credentials
from mf_jobs_harness import api_client, token_of
from mf_session_client import FakeAgent, make_hello

from meshforge.common import pki
from meshforge.common.ids import new_command_id, new_fabric_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.enrollment.service import public_key_fingerprint
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    # The subject is certificate renewal, not liveness: with the harness's
    # FAST_LIVENESS the Hello admission deadline is 0.5 s, and one descheduled
    # half-second in a long run refused a Hello "admission did not finish
    # within 0.5 s" (V0.7 G6 fix round 2: measured 7-22 ms idle, up to 117 ms
    # under 16 CPU burners). The helpers here do not retry, as an agent would.
    return make_settings(
        pg_settings, renewal={"min_interval_s": 2.0}, **tolerant_liveness()
    )


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


def _csr(
    forged: Forged,
    *,
    key: Any = None,
    node_id: str | None = None,
    fabric_id: str | None = None,
) -> bytes:
    from cryptography.hazmat.primitives import serialization

    csr = pki.build_csr(
        key or forged.key,
        fabric_id=fabric_id or pki.parse_fabric_id(_ca_cert()),
        node_id=node_id or forged.node_id,
    )
    return csr.public_bytes(serialization.Encoding.PEM)


def _ca_cert() -> Any:
    from mf_fabric import fabric_ca

    return fabric_ca().cert


async def _renew(
    rt: ControllerRuntime, forged: Forged, csr_pem: bytes
) -> tuple[agent_pb2.RenewCertificateResponse | None, grpc.StatusCode, dict[str, str]]:
    async with _channel(rt, forged) as channel:
        stub = agent_pb2_grpc.AgentControlStub(channel)
        try:
            reply = await stub.RenewCertificate(
                agent_pb2.RenewCertificateRequest(csr_pem=csr_pem), timeout=10
            )
        except grpc.aio.AioRpcError as exc:
            trailing = {
                k: v for k, v in (exc.trailing_metadata() or ()) if isinstance(v, str)
            }
            return None, exc.code(), trailing
    return reply, grpc.StatusCode.OK, {}


def _renewed(
    forged: Forged, reply: agent_pb2.RenewCertificateResponse, key: Any = None
) -> Forged:
    cert = pki.cert_from_pem(bytes(reply.node_cert_pem))
    return Forged(node_id=forged.node_id, key=key or forged.key, cert=cert)


async def _hello_status(
    rt: ControllerRuntime, forged: Forged, epoch: int
) -> tuple[grpc.StatusCode | None, str | None]:
    async with _channel(rt, forged) as channel:
        call = agent_pb2_grpc.AgentControlStub(channel).Session(timeout=10)
        try:
            await call.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    hello=make_hello(forged.node_id, epoch),
                )
            )
            reply = await call.read()
            if reply is not grpc.aio.EOF and reply.hello_ack.accepted:
                return grpc.StatusCode.OK, None
            code, trailing = await call.code(), await call.trailing_metadata()
        except grpc.aio.AioRpcError as exc:
            code, trailing = exc.code(), exc.trailing_metadata()
        finally:
            call.cancel()
    return code, dict(trailing or ()).get("meshforge-reason")


async def _connect_once(rt: ControllerRuntime, forged: Forged, epoch: int = 1) -> None:
    """One accepted session on ``forged`` (then dropped)."""
    async with _channel(rt, forged) as ch:
        agent = FakeAgent(ch, node_id=forged.node_id, epoch=epoch)
        await agent.connect()
        await agent.close()


async def _until(check: Any, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.05)


# ------------------------------------------------------------------ happy path


async def test_renew_is_pending_until_the_first_hello_with_it(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    old = forge()
    bystander = forge()
    async with _channel(runtime, old) as ch, _channel(runtime, bystander) as ch2:
        agent = FakeAgent(ch, node_id=old.node_id)
        await agent.connect()
        agent.start_heartbeats(0.2)
        other = FakeAgent(ch2, node_id=bystander.node_id)
        await other.connect()
        other.start_heartbeats(0.2)

        reply, code, trailing = await _renew(runtime, old, _csr(old))
        assert code is grpc.StatusCode.OK, trailing
        assert reply is not None
        new = _renewed(old, reply)
        assert pki.cert_fingerprint(pki.cert_from_pem(bytes(reply.ca_cert_pem))) == (
            pki.cert_fingerprint(_ca_cert())
        )
        assert pki.parse_node_id(new.cert) == old.node_id
        assert new.serial != old.serial
        rows = await _sql(
            pg_settings,
            "SELECT cert_serial, pending_cert_serial, cert_renewed_at IS NOT NULL "
            "FROM node WHERE node_id = :n",
            n=old.node_id,
        )
        assert rows == [(old.serial, new.serial, True)]
        # Not superseded yet: the live session keeps working.
        assert (
            await _sql(
                pg_settings,
                "SELECT 1 FROM revoked_cert WHERE serial = :s",
                s=old.serial,
            )
            == []
        )
        assert runtime.revocations is not None
        await runtime.revocations.sync()
        await asyncio.sleep(0.5)
        assert not agent.ended.is_set()
        audit = await _sql(
            pg_settings,
            "SELECT details->>'serial', details->>'presented_serial', "
            "details->>'key_rotated' FROM audit_event "
            "WHERE action = 'cert_renewed' AND node_id = :n",
            n=old.node_id,
        )
        assert audit == [(new.serial, old.serial, "false")]

        # The hand-over: a new stream on the new certificate (epoch 2).
        async with _channel(runtime, new) as ch3:
            successor = FakeAgent(ch3, node_id=old.node_id, epoch=2)
            await successor.connect()
            successor.start_heartbeats(0.2)
            rows = await _sql(
                pg_settings,
                "SELECT cert_serial, pending_cert_serial, cert_not_after "
                "FROM node WHERE node_id = :n",
                n=old.node_id,
            )
            assert rows[0][:2] == (new.serial, None)
            assert rows[0][2] == new.cert.not_valid_after_utc
            assert await _sql(
                pg_settings,
                "SELECT reason FROM revoked_cert WHERE serial = :s",
                s=old.serial,
            ) == [("superseded",)]
            assert (
                len(
                    await _sql(
                        pg_settings,
                        "SELECT 1 FROM audit_event WHERE action = 'cert_activated' "
                        "AND node_id = :n AND details->>'superseded_serial' = :s",
                        n=old.node_id,
                        s=old.serial,
                    )
                )
                == 1
            )

            # The set is re-synced at once (no waiting for the poll): the
            # bystander is told the old serial.
            def told() -> bool:
                return any(
                    m.WhichOneof("payload") == "revocation_update"
                    and old.serial in m.revocation_update.serials
                    for m in other.received
                )

            async def told_async() -> bool:
                return told()

            await _until(told_async, timeout_s=3.0)
            assert old.serial in runtime.revocations.revoked_serials()
            assert runtime.revocations.revoked_serials()[old.serial] == "superseded"
            assert not successor.ended.is_set()
            await successor.close()
        await agent.close()
        await other.close()
    # From now on the old certificate is refused, superseded.
    code, reason = await _hello_status(runtime, old, epoch=9)
    assert (code, reason) == (grpc.StatusCode.PERMISSION_DENIED, "cert_superseded")


# -------------------------------------------------------------------- refusals


async def test_renew_refusals_are_typed_and_issue_nothing(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    me = forge()
    await _connect_once(runtime, me)
    victim = forge()
    cases: list[tuple[bytes, grpc.StatusCode, str]] = [
        (
            _csr(me, node_id=victim.node_id),
            grpc.StatusCode.PERMISSION_DENIED,
            "renew_node_mismatch",
        ),
        (
            _csr(me, fabric_id=str(new_fabric_id())),
            grpc.StatusCode.INVALID_ARGUMENT,
            "csr_fabric_mismatch",
        ),
        (b"not a csr", grpc.StatusCode.INVALID_ARGUMENT, "csr_malformed"),
        (b"", grpc.StatusCode.INVALID_ARGUMENT, "csr_malformed"),
    ]
    for csr_pem, want_code, want_reason in cases:
        reply, code, trailing = await _renew(runtime, me, csr_pem)
        assert reply is None
        assert (code, trailing.get("meshforge-reason")) == (want_code, want_reason)
    rows = await _sql(
        pg_settings,
        "SELECT pending_cert_serial, cert_renewed_at FROM node WHERE node_id = :n",
        n=me.node_id,
    )
    assert rows == [(None, None)]
    assert (
        len(
            await _sql(
                pg_settings,
                "SELECT 1 FROM audit_event WHERE action = 'cert_renew_refused' "
                "AND details->>'reason' = 'renew_node_mismatch'",
            )
        )
        == 1
    )

    # Rate limit: one issuance per node per min_interval_s (2 s here).
    reply, code, _ = await _renew(runtime, me, _csr(me))
    assert code is grpc.StatusCode.OK and reply is not None
    reply2, code, trailing = await _renew(runtime, me, _csr(me))
    assert reply2 is None
    assert code is grpc.StatusCode.RESOURCE_EXHAUSTED
    assert trailing.get("meshforge-reason") == "rate_limited"
    assert 1 <= int(trailing["meshforge-retry-after-s"]) <= 3
    await asyncio.sleep(2.1)
    reply3, code, _ = await _renew(runtime, me, _csr(me))
    assert code is grpc.StatusCode.OK and reply3 is not None
    # The first (never used) pending certificate is superseded by the second.
    first = _renewed(me, reply)
    assert await _sql(
        pg_settings, "SELECT reason FROM revoked_cert WHERE serial = :s", s=first.serial
    ) == [("superseded",)]


async def test_an_expired_certificate_cannot_even_reach_renewal(
    runtime: ControllerRuntime,
) -> None:
    old = forge(
        now=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2),
        lifetime=dt.timedelta(minutes=1),
    )
    _reply, code, _ = await _renew(runtime, old, _csr(old))
    assert code is grpc.StatusCode.UNAVAILABLE  # the TLS handshake failed


async def test_a_superseded_or_revoked_certificate_cannot_renew(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    old = forge()
    await _connect_once(runtime, old)
    reply, code, _ = await _renew(runtime, old, _csr(old))
    assert reply is not None
    new = _renewed(old, reply)
    await _connect_once(runtime, new, epoch=2)
    assert runtime.revocations is not None
    await runtime.revocations.sync()
    # The old (superseded) certificate is refused by the interceptor.
    _reply, code, trailing = await _renew(runtime, old, _csr(old))
    assert (code, trailing.get("meshforge-reason")) == (
        grpc.StatusCode.PERMISSION_DENIED,
        "cert_superseded",
    )
    # A certificate the node row never named (CA-valid, not listed): refused
    # by renewal's own check.
    stray = forge(node_id=old.node_id)
    _reply, code, trailing = await _renew(runtime, stray, _csr(stray))
    assert (code, trailing.get("meshforge-reason")) == (
        grpc.StatusCode.PERMISSION_DENIED,
        "cert_superseded",
    )
    # Revoked: the interceptor refuses at once.
    async with api_client(runtime, token=token_of(runtime)) as http:
        assert (await http.post(f"/v1/nodes/{old.node_id}/revoke")).status_code == 200
    await asyncio.sleep(2.1)  # past the rate limit, so only revocation can refuse
    _reply, code, trailing = await _renew(runtime, new, _csr(new))
    assert (code, trailing.get("meshforge-reason")) == (
        grpc.StatusCode.PERMISSION_DENIED,
        "cert_revoked",
    )


async def test_renewal_racing_revocation_never_leaves_a_live_certificate(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    """Both lock the node row: whichever commits first, every certificate the
    node holds or was issued ends up in the revocation set."""
    for _ in range(6):
        node = forge()
        await _connect_once(runtime, node)

        async def revoke(node_id: str = node.node_id) -> int:
            async with api_client(runtime, token=token_of(runtime)) as http:
                return (await http.post(f"/v1/nodes/{node_id}/revoke")).status_code

        results = await asyncio.gather(
            _renew(runtime, node, _csr(node)), revoke(), return_exceptions=True
        )
        renew_result, revoke_status = results
        assert revoke_status == 200
        assert not isinstance(renew_result, BaseException), renew_result
        reply, _code, trailing = renew_result
        listed = {
            r[0]
            for r in await _sql(
                pg_settings,
                "SELECT serial FROM revoked_cert WHERE node_id = :n",
                n=node.node_id,
            )
        }
        assert node.serial in listed
        if reply is not None:
            assert _renewed(node, reply).serial in listed, "issued but not revoked"
        else:
            assert trailing.get("meshforge-reason") in {"node_revoked", "cert_revoked"}


async def test_key_rotation_certifies_the_new_key_and_records_it_at_activation(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    old = forge()
    await _connect_once(runtime, old)
    new_key = pki.generate_key()
    reply, code, _ = await _renew(runtime, old, _csr(old, key=new_key))
    assert code is grpc.StatusCode.OK and reply is not None
    new = _renewed(old, reply, key=new_key)
    assert pki.public_keys_equal(new_key.public_key(), new.cert.public_key())
    old_fp = public_key_fingerprint(old.key.public_key())
    new_fp = public_key_fingerprint(new_key.public_key())
    assert await _sql(
        pg_settings,
        "SELECT public_key_fingerprint, pending_public_key_fingerprint FROM node "
        "WHERE node_id = :n",
        n=old.node_id,
    ) == [(old_fp, new_fp)]
    audit = await _sql(
        pg_settings,
        "SELECT details->>'key_rotated' FROM audit_event "
        "WHERE action = 'cert_renewed' AND node_id = :n",
        n=old.node_id,
    )
    assert audit == [("true",)]
    await _connect_once(runtime, new, epoch=2)
    assert await _sql(
        pg_settings,
        "SELECT public_key_fingerprint, cert_serial FROM node WHERE node_id = :n",
        n=old.node_id,
    ) == [(new_fp, new.serial)]


async def test_revoking_a_node_also_revokes_its_unused_pending_certificate(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    node = forge()
    await _connect_once(runtime, node)
    reply, _code, _ = await _renew(runtime, node, _csr(node))
    assert reply is not None
    pending = _renewed(node, reply)
    async with api_client(runtime, token=token_of(runtime)) as http:
        body = (await http.post(f"/v1/nodes/{node.node_id}/revoke")).json()
    assert body["state"] == "revoked"
    listed = dict(
        await _sql(
            pg_settings,
            "SELECT serial, reason FROM revoked_cert WHERE node_id = :n",
            n=node.node_id,
        )
    )
    assert listed == {node.serial: "revoked", pending.serial: "revoked"}
    code, reason = await _hello_status(runtime, pending, epoch=5)
    assert (code, reason) == (grpc.StatusCode.PERMISSION_DENIED, "cert_revoked")
