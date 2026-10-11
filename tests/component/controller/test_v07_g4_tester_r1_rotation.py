"""V0.7 G4 tester round 1: certificate rotation, derived from D140 / D155 /
brief 5d item 1 - not from the implementation.

Real PostgreSQL, the real ``ControllerRuntime`` (mutual TLS), real gRPC.

* a controller restart while a renewed certificate is still pending: the old
  certificate keeps working (the agent crashed before its swap), the pending
  one activates on a freshly started controller, and after the next restart the
  superseded serial is refused from the very first connection;
* a chain of rotations (gen0 -> gen1 -> gen2): every earlier generation is
  refused, exactly one certificate is current, the audit trail is complete;
* a pending certificate that a newer renewal replaced can never activate;
* a burst of concurrent renewals issues exactly one certificate;
* hostile CSRs: extensions asking for CA powers / another node's identity,
  a key that is not P-256, a tampered signature, an oversized request, and a
  call with no client certificate at all;
* issued certificates never exceed the configured lifetime and never carry
  more than the node's own identity.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from mf_controller_harness import make_settings
from mf_fabric import ca_pem, fabric_ca
from mf_forged_certs import Forged, forge, grpc_credentials
from mf_session_client import FakeAgent, make_hello

from meshforge.common import pki
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

MIN_INTERVAL_S = 1.5
_SUPERSEDED = {"cert_superseded", "cert_revoked"}


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_settings(pg_settings, renewal={"min_interval_s": MIN_INTERVAL_S})


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


def _csr_pem(forged: Forged, *, key: Any = None, node_id: str | None = None) -> bytes:
    csr = pki.build_csr(
        key or forged.key,
        fabric_id=fabric_ca().fabric_id,
        node_id=node_id or forged.node_id,
    )
    return csr.public_bytes(serialization.Encoding.PEM)


async def _renew(
    rt: ControllerRuntime, forged: Forged, csr_pem: bytes
) -> tuple[agent_pb2.RenewCertificateResponse | None, grpc.StatusCode, dict[str, str]]:
    async with _channel(rt, forged) as channel:
        stub = agent_pb2_grpc.AgentControlStub(channel)
        try:
            reply = await stub.RenewCertificate(
                agent_pb2.RenewCertificateRequest(csr_pem=csr_pem), timeout=15
            )
        except grpc.aio.AioRpcError as exc:
            trailing = {
                k: v for k, v in (exc.trailing_metadata() or ()) if isinstance(v, str)
            }
            return None, exc.code(), trailing
    return reply, grpc.StatusCode.OK, {}


def _next(forged: Forged, reply: agent_pb2.RenewCertificateResponse) -> Forged:
    return Forged(
        node_id=forged.node_id,
        key=forged.key,
        cert=pki.cert_from_pem(bytes(reply.node_cert_pem)),
    )


async def _hello(
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


async def _connect(rt: ControllerRuntime, forged: Forged, epoch: int) -> None:
    async with _channel(rt, forged) as ch:
        agent = FakeAgent(ch, node_id=forged.node_id, epoch=epoch)
        await agent.connect()
        await agent.close()


# ------------------------------------------------------------------ restart


async def test_controller_restart_with_a_pending_certificate_keeps_both_paths_safe(
    settings: ControllerSettings, pg_settings: DatabaseSettings
) -> None:
    """Criterion: 'crash before the swap keeps the old cert working'; 'old cert
    rejected after cutover' - across controller restarts."""
    old = forge()
    async with ControllerRuntime.start(settings) as rt1:
        await _connect(rt1, old, epoch=1)
        reply, code, _ = await _renew(rt1, old, _csr_pem(old))
        assert code is grpc.StatusCode.OK and reply is not None
        new = _next(old, reply)
    # Controller down with a pending certificate on file.
    async with ControllerRuntime.start(settings) as rt2:
        # The agent never swapped (crashed): the old certificate still works.
        assert await _hello(rt2, old, epoch=2) == (grpc.StatusCode.OK, None)
        rows = await _sql(
            pg_settings,
            "SELECT cert_serial, pending_cert_serial FROM node WHERE node_id = :n",
            n=old.node_id,
        )
        assert rows == [(old.serial, new.serial)], "pending must survive a restart"
        assert (
            await _sql(
                pg_settings,
                "SELECT 1 FROM revoked_cert WHERE serial = :s",
                s=old.serial,
            )
            == []
        )
        # The pending certificate activates on the restarted controller.
        assert await _hello(rt2, new, epoch=3) == (grpc.StatusCode.OK, None)
        rows = await _sql(
            pg_settings,
            "SELECT cert_serial, pending_cert_serial FROM node WHERE node_id = :n",
            n=old.node_id,
        )
        assert rows == [(new.serial, None)]
    # A second restart: the superseded serial is refused from the FIRST connect.
    async with ControllerRuntime.start(settings) as rt3:
        code, reason = await _hello(rt3, old, epoch=4)
        assert code is not grpc.StatusCode.OK and reason in _SUPERSEDED, (code, reason)
        assert await _hello(rt3, new, epoch=5) == (grpc.StatusCode.OK, None)


async def test_a_pending_certificate_unused_through_a_restart_is_still_refused_once_replaced(
    settings: ControllerSettings, pg_settings: DatabaseSettings
) -> None:
    node = forge()
    async with ControllerRuntime.start(settings) as rt1:
        await _connect(rt1, node, epoch=1)
        first, code, _ = await _renew(rt1, node, _csr_pem(node))
        assert code is grpc.StatusCode.OK and first is not None
        gen1a = _next(node, first)
    async with ControllerRuntime.start(settings) as rt2:
        await asyncio.sleep(MIN_INTERVAL_S + 0.2)
        second, code, _ = await _renew(rt2, node, _csr_pem(node))
        assert code is grpc.StatusCode.OK and second is not None
        gen1b = _next(node, second)
        assert gen1a.serial != gen1b.serial
        # The replaced pending certificate can never become current.
        code, reason = await _hello(rt2, gen1a, epoch=2)
        assert code is not grpc.StatusCode.OK and reason in _SUPERSEDED, (code, reason)
        assert await _hello(rt2, gen1b, epoch=3) == (grpc.StatusCode.OK, None)
        rows = await _sql(
            pg_settings,
            "SELECT cert_serial, pending_cert_serial FROM node WHERE node_id = :n",
            n=node.node_id,
        )
        assert rows == [(gen1b.serial, None)]


# --------------------------------------------------------------- double rotation


async def test_a_chain_of_rotations_leaves_exactly_one_current_certificate(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    gen0 = forge()
    await _connect(runtime, gen0, epoch=1)
    reply, code, _ = await _renew(runtime, gen0, _csr_pem(gen0))
    assert reply is not None, code
    gen1 = _next(gen0, reply)
    await _connect(runtime, gen1, epoch=2)
    assert runtime.revocations is not None
    await runtime.revocations.sync()
    await asyncio.sleep(MIN_INTERVAL_S + 0.2)
    # Second rotation, with a NEW key this time (rotate-key after a renewal).
    key2 = pki.generate_key()
    reply2, code, trailing = await _renew(runtime, gen1, _csr_pem(gen1, key=key2))
    assert reply2 is not None, (code, trailing)
    gen2 = Forged(gen0.node_id, key2, pki.cert_from_pem(bytes(reply2.node_cert_pem)))
    await _connect(runtime, gen2, epoch=3)
    await runtime.revocations.sync()

    listed = dict(await _sql(pg_settings, "SELECT serial, reason FROM revoked_cert"))
    assert listed.get(gen0.serial) == "superseded"
    assert listed.get(gen1.serial) == "superseded"
    assert gen2.serial not in listed
    for epoch, stale in ((10, gen0), (11, gen1)):
        code_, reason = await _hello(runtime, stale, epoch)
        assert code_ is not grpc.StatusCode.OK and reason in _SUPERSEDED, (
            code_,
            reason,
        )
    # Stale generations cannot renew either (cannot mint a lineage).
    for stale in (gen0, gen1):
        _r, code_, trailing = await _renew(runtime, stale, _csr_pem(stale))
        assert code_ is not grpc.StatusCode.OK, trailing
        assert trailing.get("meshforge-reason") in _SUPERSEDED, trailing
    assert await _hello(runtime, gen2, epoch=12) == (grpc.StatusCode.OK, None)
    rows = await _sql(
        pg_settings,
        "SELECT cert_serial, pending_cert_serial FROM node WHERE node_id = :n",
        n=gen0.node_id,
    )
    assert rows == [(gen2.serial, None)]
    actions = [
        r[0]
        for r in await _sql(
            pg_settings,
            "SELECT action FROM audit_event WHERE node_id = :n "
            "AND action IN ('cert_renewed', 'cert_activated') ORDER BY id",
            n=gen0.node_id,
        )
    ]
    assert actions.count("cert_renewed") == 2 and actions.count("cert_activated") == 2


async def test_concurrent_renewals_issue_exactly_one_certificate(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    node = forge()
    await _connect(runtime, node, epoch=1)
    results = await asyncio.gather(
        *[_renew(runtime, node, _csr_pem(node)) for _ in range(8)]
    )
    issued = [r for r in results if r[0] is not None]
    refused = [r for r in results if r[0] is None]
    assert len(issued) == 1, [(r[1], r[2]) for r in results]
    assert {r[1] for r in refused} == {grpc.StatusCode.RESOURCE_EXHAUSTED}
    assert all("meshforge-retry-after-s" in r[2] for r in refused)
    assert await _sql(
        pg_settings,
        "SELECT count(*) FROM audit_event WHERE action = 'cert_renewed' "
        "AND node_id = :n",
        n=node.node_id,
    ) == [(1,)]


# ------------------------------------------------------------- hostile requests


def _custom_csr(
    key: Any,
    *,
    node_id: str,
    extensions: list[tuple[x509.ExtensionType, bool]],
    signer: Any = None,
    fabric_id: str | None = None,
) -> bytes:
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    )
    for extension, critical in extensions:
        builder = builder.add_extension(extension, critical=critical)
    csr = builder.sign(signer or key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM)


async def test_a_csr_cannot_smuggle_ca_powers_or_another_identity_into_the_certificate(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    me, victim = forge(), forge()
    await _connect(runtime, me, epoch=1)
    fabric = fabric_ca().fabric_id
    my_uri = x509.UniformResourceIdentifier(pki.node_uri(fabric, me.node_id))
    victim_uri = x509.UniformResourceIdentifier(pki.node_uri(fabric, victim.node_id))
    cases = {
        "ca_basic_constraints": [
            (x509.SubjectAlternativeName([my_uri]), False),
            (x509.BasicConstraints(ca=True, path_length=None), True),
            (
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            ),
        ],
        "second_identity_san": [
            (x509.SubjectAlternativeName([my_uri, victim_uri]), False)
        ],
        "server_name_san": [
            (x509.SubjectAlternativeName([my_uri, x509.DNSName("controller")]), False)
        ],
    }
    for label, extensions in cases.items():
        pem = _custom_csr(me.key, node_id=me.node_id, extensions=extensions)
        reply, code, trailing = await _renew(runtime, me, pem)
        if reply is None:
            # Refusing a hostile CSR outright is the strictest correct answer.
            assert code in (
                grpc.StatusCode.INVALID_ARGUMENT,
                grpc.StatusCode.PERMISSION_DENIED,
            ), (label, code, trailing)
            continue
        cert = pki.cert_from_pem(bytes(reply.node_cert_pem))
        sans = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        assert list(sans) == [my_uri], (label, list(sans))
        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        assert basic.ca is False, label
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert set(eku) <= {
            ExtendedKeyUsageOID.CLIENT_AUTH,
            ExtendedKeyUsageOID.SERVER_AUTH,
        }, label
        await asyncio.sleep(MIN_INTERVAL_S + 0.2)  # next case may renew again


async def test_issued_lifetime_is_the_configured_one_and_never_longer_than_the_ca(
    runtime: ControllerRuntime, settings: ControllerSettings
) -> None:
    node = forge()
    await _connect(runtime, node, epoch=1)
    reply, _code, _ = await _renew(runtime, node, _csr_pem(node))
    assert reply is not None
    cert = pki.cert_from_pem(bytes(reply.node_cert_pem))
    life = cert.not_valid_after_utc - cert.not_valid_before_utc
    cfg = dt.timedelta(seconds=settings.controller.renewal.node_cert_lifetime_s)
    assert life <= cfg + pki.CLOCK_SKEW + dt.timedelta(minutes=1), life
    assert cert.not_valid_after_utc <= fabric_ca().cert.not_valid_after_utc
    assert cert.not_valid_before_utc <= dt.datetime.now(dt.UTC)


@pytest.mark.parametrize("kind", ["rsa", "p384", "ed25519"])
async def test_only_p256_keys_can_be_certified(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings, kind: str
) -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    me = forge()
    await _connect(runtime, me, epoch=1)
    key: Any
    if kind == "rsa":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        algo: Any = hashes.SHA256()
    elif kind == "p384":
        key = ec.generate_private_key(ec.SECP384R1())
        algo = hashes.SHA384()
    else:
        key = ed25519.Ed25519PrivateKey.generate()
        algo = None
    uri = x509.UniformResourceIdentifier(
        pki.node_uri(fabric_ca().fabric_id, me.node_id)
    )
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, me.node_id)]))
        .add_extension(x509.SubjectAlternativeName([uri]), critical=False)
        .sign(key, algo)
    )
    reply, code, trailing = await _renew(
        runtime, me, csr.public_bytes(serialization.Encoding.PEM)
    )
    assert reply is None, f"a {kind} key was certified"
    assert code is grpc.StatusCode.INVALID_ARGUMENT, (code, trailing)
    assert trailing.get("meshforge-reason", "").startswith("csr_")
    assert await _sql(
        pg_settings,
        "SELECT pending_cert_serial FROM node WHERE node_id = :n",
        n=me.node_id,
    ) == [(None,)]


async def test_a_tampered_csr_signature_is_refused(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    me = forge()
    await _connect(runtime, me, epoch=1)
    other_key = pki.generate_key()
    # A CSR that names MY key but was signed with another one: proof of
    # possession must fail.
    uri = x509.UniformResourceIdentifier(
        pki.node_uri(fabric_ca().fabric_id, me.node_id)
    )
    good = pki.build_csr(me.key, fabric_id=fabric_ca().fabric_id, node_id=me.node_id)
    der = bytearray(good.public_bytes(serialization.Encoding.DER))
    der[-3] ^= 0xFF  # flip bits inside the signature
    pem = (
        b"-----BEGIN CERTIFICATE REQUEST-----\n"
        + __import__("base64").encodebytes(bytes(der))
        + b"-----END CERTIFICATE REQUEST-----\n"
    )
    reply, code, trailing = await _renew(runtime, me, pem)
    assert reply is None and code in (
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.PERMISSION_DENIED,
    ), (code, trailing)
    del other_key, uri
    assert await _sql(
        pg_settings,
        "SELECT pending_cert_serial FROM node WHERE node_id = :n",
        n=me.node_id,
    ) == [(None,)]


async def test_an_oversized_csr_is_refused_and_the_controller_stays_up(
    runtime: ControllerRuntime,
) -> None:
    me = forge()
    await _connect(runtime, me, epoch=1)
    for size in (64 * 1024, 3 * 1024 * 1024):
        reply, code, trailing = await _renew(
            runtime,
            me,
            b"-----BEGIN CERTIFICATE REQUEST-----\n" + b"A" * size,
        )
        assert reply is None
        assert code in (
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.RESOURCE_EXHAUSTED,
        ), (size, code, trailing)
    # Still serving.
    assert await _hello(runtime, me, epoch=2) == (grpc.StatusCode.OK, None)


async def test_renewal_without_a_client_certificate_is_impossible(
    runtime: ControllerRuntime,
) -> None:
    creds = grpc.ssl_channel_credentials(root_certificates=ca_pem())
    me = forge()
    async with grpc.aio.secure_channel(
        f"127.0.0.1:{runtime.grpc_port}",
        creds,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        stub = agent_pb2_grpc.AgentControlStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await stub.RenewCertificate(
                agent_pb2.RenewCertificateRequest(csr_pem=_csr_pem(me)), timeout=10
            )
    assert raised.value.code() in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.PERMISSION_DENIED,
    )
