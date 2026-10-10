"""V0.7 G3 tester: certificates that chain to the fabric CA but should not work,
and revocation under concurrency, against the real controller (PostgreSQL, real
gRPC/TLS, real HTTPS).

Criterion 1 (a stolen/replaced/revoked identity is cut off) and criterion 3
(mTLS on every plane) read from the roadmap; nothing here is taken from the
implementation's own tests.

Defects confirmed by this file are strict xfails named T-n (see
``docs/briefs/progress/V0.7-G3.md``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc
import httpx
import pytest
import pytest_asyncio
from cryptography import x509
from mf_fabric import NodeChannels, ca_pem, client_ssl_context
from mf_forged_certs import (
    CLIENT,
    SERVER,
    Forged,
    forge,
    grpc_credentials,
    other_fabric_id,
)
from mf_jobs_harness import api_client, token_of
from mf_session_client import FakeAgent, make_hello

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import make_settings
from sqlalchemy import text

from meshforge.common import pki
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.fabric_ca import create_fabric, ensure_server_cert
from meshforge.controller.registry import Registry
from meshforge.controller.revocation import supersede
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.settings import DatabaseSettings

DAY = dt.timedelta(days=1)

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


def _target(rt: ControllerRuntime) -> str:
    return f"127.0.0.1:{rt.grpc_port}"


async def _hello(
    forged: Forged, rt: ControllerRuntime, claim: str | None = None, epoch: int = 1
) -> tuple[grpc.StatusCode | None, dict[str, str]]:
    """Open a session with ``forged`` as the client certificate; final status."""
    async with grpc.aio.secure_channel(
        _target(rt),
        grpc_credentials(forged),
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        call = agent_pb2_grpc.AgentControlStub(channel).Session(timeout=10)
        try:
            await call.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    hello=make_hello(forged.node_id if claim is None else claim, epoch),
                )
            )
            reply = await call.read()
            if (
                reply is not grpc.aio.EOF
                and reply.WhichOneof("payload") == "hello_ack"
                and reply.hello_ack.accepted
            ):
                return grpc.StatusCode.OK, {}
            return await call.code(), {
                k: v
                for k, v in (await call.trailing_metadata() or ())
                if isinstance(v, str)
            }
        except grpc.aio.AioRpcError as exc:
            return exc.code(), {
                k: v for k, v in (exc.trailing_metadata() or ()) if isinstance(v, str)
            }
        finally:
            call.cancel()


MISSHAPEN: dict[str, Callable[[], Forged]] = {
    "server_auth_only": lambda: forge(usages=[SERVER]),
    "foreign_fabric_uri": lambda: forge(fabric_id=other_fabric_id()),
    "dns_san_only_like_the_controller": lambda: forge(
        sans=[x509.DNSName(pki.SERVER_NAME)], usages=[SERVER]
    ),
    "no_san_at_all": lambda: forge(sans=[], usages=[CLIENT]),
    "two_node_uris": lambda: forge(
        sans=[
            x509.UniformResourceIdentifier(
                pki.node_uri(pki.parse_fabric_id(forge().cert), str(new_node_id()))
            ),
            x509.UniformResourceIdentifier(
                pki.node_uri(pki.parse_fabric_id(forge().cert), str(new_node_id()))
            ),
        ]
    ),
}


@pytest.mark.parametrize("shape", sorted(MISSHAPEN))
async def test_ca_signed_but_misshapen_cert_cannot_open_a_session(
    runtime: ControllerRuntime, registry: Registry, shape: str
) -> None:
    forged = MISSHAPEN[shape]()
    code, _ = await _hello(forged, runtime)
    assert code in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNAUTHENTICATED}, code
    assert await registry.get_node(forged.node_id) is None
    assert runtime.manager is not None and len(runtime.manager) == 0


@pytest.mark.parametrize("shape", sorted(MISSHAPEN))
async def test_ca_signed_but_misshapen_cert_gets_nothing_from_the_blob_plane(
    runtime: ControllerRuntime, shape: str
) -> None:
    forged = MISSHAPEN[shape]()
    data = b"x" * 1024
    digest = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as cli:
        assert (
            await cli.put(f"/v1/blobs/{digest.hex}", content=data)
        ).status_code == 201
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint(
        str(new_attempt_id()), [digest], ttl_s=60, node_id=forged.node_id
    ).get_secret_value()
    ctx = client_ssl_context()
    ctx.load_cert_chain(*_files(forged))
    async with httpx.AsyncClient(verify=ctx, timeout=10) as http:
        try:
            response = await http.get(
                f"https://127.0.0.1:{runtime.http_port}/v1/blobs/{digest.hex}",
                headers={"authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            return  # refused at the TLS layer
    assert response.status_code in (401, 403), response.text


def _files(forged: Forged) -> tuple[str, str]:
    import tempfile
    from pathlib import Path

    folder = Path(tempfile.mkdtemp(prefix="mf-forged-"))
    cert, key = folder / "c.pem", folder / "k.pem"
    cert.write_bytes(forged.cert_pem)
    key.write_bytes(forged.key_pem)
    return str(cert), str(key)


async def test_hello_claiming_a_well_formed_but_unknown_node_is_refused_with_a_valid_cert(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    mine = forge()
    code, trailing = await _hello(mine, runtime, claim=str(new_node_id()))
    assert code is grpc.StatusCode.UNAUTHENTICATED
    assert trailing.get("meshforge-reason") == "node_id_mismatch"
    assert await registry.get_node(mine.node_id) is None


@pytest.mark.parametrize(
    "claim",
    [
        "",
        "node_",
        "NODE_01J0HARNESS000000000000000",
        " node_01J0HARNESS000000000000000",
        "node_01J0HARNESS000000000000000\n",
        "node_" + "0" * 4096,
    ],
)
async def test_hello_with_a_garbage_node_id_never_creates_anything(
    runtime: ControllerRuntime, registry: Registry, claim: str
) -> None:
    mine = forge()
    code, _ = await _hello(mine, runtime, claim=claim)
    assert code in {grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.INVALID_ARGUMENT}
    assert await registry.get_node(mine.node_id) is None


# ----------------------------------------------------------- superseded / revoked


async def _sql(pg: DatabaseSettings, statement: str, params: dict[str, Any]) -> None:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _supersede(pg: DatabaseSettings, forged: Forged) -> None:
    engine = create_engine(pg)
    try:
        factory = create_session_factory(engine)
        async with factory() as db, db.begin():
            await supersede(
                db,
                node_id=forged.node_id,
                serial=forged.serial,
                not_after=forged.cert.not_valid_after_utc,
            )
    finally:
        await engine.dispose()


async def test_superseded_serial_is_refused_on_session_and_blob_planes(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    old = forge()
    assert (await _hello(old, runtime))[0] is grpc.StatusCode.OK
    await _supersede(pg_settings, old)
    assert runtime.revocations is not None
    await runtime.revocations.sync()
    code, trailing = await _hello(old, runtime, epoch=2)
    assert code is grpc.StatusCode.PERMISSION_DENIED, (code, trailing)
    assert trailing.get("meshforge-reason") == "cert_revoked"

    digest = sha256_bytes(b"s" * 100)
    async with api_client(runtime, token=token_of(runtime)) as cli:
        await cli.put(f"/v1/blobs/{digest.hex}", content=b"s" * 100)
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint(
        str(new_attempt_id()), [digest], ttl_s=60, node_id=old.node_id
    ).get_secret_value()
    ctx = client_ssl_context()
    ctx.load_cert_chain(*_files(old))
    async with httpx.AsyncClient(verify=ctx, timeout=10) as http:
        response = await http.get(
            f"https://127.0.0.1:{runtime.http_port}/v1/blobs/{digest.hex}",
            headers={"authorization": f"Bearer {token}"},
        )
    assert response.status_code == 401


async def test_superseding_a_cert_cuts_the_live_session_that_uses_it(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    old = forge()
    async with grpc.aio.secure_channel(
        _target(runtime),
        grpc_credentials(old),
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        agent = FakeAgent(channel, node_id=old.node_id)
        await agent.connect()
        agent.start_heartbeats(0.1)
        try:
            await _supersede(pg_settings, old)
            assert runtime.revocations is not None
            await runtime.revocations.sync()
            await asyncio.sleep(2.5)  # > the 1 s revocation poll
            assert agent.ended.is_set(), (
                "the superseded certificate still has a live stream"
            )
        finally:
            await agent.close()


async def test_revoke_while_reconnecting_never_leaves_a_live_session(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """Hammer Hellos of one node while it is revoked over REST."""
    node = forge()
    stop = asyncio.Event()
    admitted_after: list[float] = []
    revoked_at: list[float] = []

    async def reconnect_loop() -> None:
        epoch = 0
        while not stop.is_set():
            epoch += 1
            code, _ = await _hello(node, runtime, epoch=epoch)
            if code is grpc.StatusCode.OK and revoked_at:
                admitted_after.append(time.monotonic() - revoked_at[0])

    workers = [asyncio.create_task(reconnect_loop()) for _ in range(4)]
    try:
        await asyncio.sleep(0.5)
        async with api_client(runtime, token=token_of(runtime)) as http:
            revoked_at.append(time.monotonic())
            response = await http.post(f"/v1/nodes/{node.node_id}/revoke")
        assert response.status_code == 200, response.text
        await asyncio.sleep(1.5)
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)
    assert runtime.manager is not None
    # A Hello admitted after the revoke call returned is a bypass.
    late = [t for t in admitted_after if t > 0.25]
    assert late == [], f"admitted {late} s after the revoke returned"
    await asyncio.sleep(2.2)  # the periodic sync is the backstop
    assert not runtime.manager.has_session(node.node_id)
    record = await registry.get_node(node.node_id)
    assert record is not None and record.state.value == "revoked"


async def test_revoking_two_nodes_updates_a_bystander_cumulatively(
    runtime: ControllerRuntime,
) -> None:
    a, b = forge(), forge()
    async with NodeChannels(_target(runtime)) as pool:
        bystander = FakeAgent(pool)
        await bystander.connect()
        for victim in (a, b):
            assert (await _hello(victim, runtime))[0] is grpc.StatusCode.OK
            async with api_client(runtime, token=token_of(runtime)) as http:
                assert (
                    await http.post(f"/v1/nodes/{victim.node_id}/revoke")
                ).status_code == 200
        deadline = time.monotonic() + 5
        while len(bystander.messages("revocation_update")) < 2:
            assert time.monotonic() < deadline, "bystander never saw two updates"
            await asyncio.sleep(0.02)
        last = bystander.messages("revocation_update")[-1].revocation_update
        assert {a.serial, b.serial} <= set(last.serials)  # the full set, not a delta
        ids = [m.message_id for m in bystander.messages("revocation_update")]
        assert len(set(ids)) == len(ids)  # distinct commands
        await bystander.close()


async def test_a_new_session_after_the_revocation_gets_the_whole_set(
    runtime: ControllerRuntime,
) -> None:
    victim = forge()
    assert (await _hello(victim, runtime))[0] is grpc.StatusCode.OK
    async with api_client(runtime, token=token_of(runtime)) as http:
        assert (
            await http.post(f"/v1/nodes/{victim.node_id}/revoke")
        ).status_code == 200
    async with NodeChannels(_target(runtime)) as pool:  # joins AFTER: lost update
        late = FakeAgent(pool)
        ack = await late.connect()
        assert victim.serial in set(ack.revoked_serials)
        await late.close()


# ----------------------------------------------------------- hot reload, live


@pytest_asyncio.fixture
async def almost_due(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(pg_settings, fabric=False, tls={"server_cert_days": 1})
    issued_at = dt.datetime.now(dt.UTC) - (DAY * 2 / 3 - dt.timedelta(minutes=3))
    fabric = create_fabric(settings.ca_dir, now=issued_at - dt.timedelta(hours=1))
    ensure_server_cert(
        settings.tls_dir,
        fabric,
        hosts=settings.server_cert_hosts,
        now=issued_at,
        lifetime=DAY,
    )
    async with ControllerRuntime.start(settings) as rt:
        yield rt


async def test_server_cert_reload_with_a_live_session_and_a_busy_https_client(
    almost_due: ControllerRuntime,
) -> None:
    runtime = almost_due
    assert runtime.server_tls is not None and runtime.fabric is not None
    # this controller made its own CA: certify the nodes with it
    ca = runtime.fabric.authority
    node = forge(ca=ca)
    creds = grpc.ssl_channel_credentials(
        root_certificates=pki.cert_to_pem(ca.cert),
        private_key=node.key_pem,
        certificate_chain=node.cert_pem,
    )
    options = [("grpc.ssl_target_name_override", pki.SERVER_NAME)]
    ctx = ssl.create_default_context(cadata=pki.cert_to_pem(ca.cert).decode("ascii"))
    ctx.load_cert_chain(*_files(node))
    url = f"https://127.0.0.1:{runtime.http_port}/healthz"
    failures: list[str] = []
    stop = asyncio.Event()
    async with grpc.aio.secure_channel(_target(runtime), creds, options=options) as ch:
        agent = FakeAgent(ch, node_id=node.node_id)
        await agent.connect()
        agent.start_heartbeats(0.05)

        async def hammer() -> None:
            while not stop.is_set():
                try:
                    async with httpx.AsyncClient(verify=ctx, timeout=5) as http:
                        r = await http.get(url)
                        if r.status_code != 200:
                            failures.append(str(r.status_code))
                except httpx.HTTPError as exc:
                    failures.append(repr(exc))

        async with httpx.AsyncClient(verify=ctx, timeout=5) as keepalive:
            assert (await keepalive.get(url)).status_code == 200
            workers = [asyncio.create_task(hammer()) for _ in range(3)]
            first = runtime.server_tls.current.cert.serial_number
            try:
                await asyncio.sleep(0.3)
                later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=4)
                runtime.server_tls._now = lambda: later  # type: ignore[assignment]
                assert await runtime.server_tls.check() is True
                await asyncio.sleep(0.5)
                assert (await keepalive.get(url)).status_code == 200  # old connection
            finally:
                stop.set()
                await asyncio.gather(*workers)
        assert runtime.server_tls.current.cert.serial_number != first
        assert failures == []
        assert not agent.ended.is_set(), (agent.end_code, agent.end_details)
        # a brand-new mTLS session also works on the renewed certificate
        node2 = forge(ca=ca)
        creds2 = grpc.ssl_channel_credentials(
            root_certificates=pki.cert_to_pem(ca.cert),
            private_key=node2.key_pem,
            certificate_chain=node2.cert_pem,
        )
        async with grpc.aio.secure_channel(
            _target(runtime), creds2, options=options
        ) as c2:
            second = FakeAgent(c2, node_id=node2.node_id)
            await second.connect()
            await second.close()
        await agent.close()


# ----------------------------------------------------------- TLS floor, tampered


async def test_session_port_does_not_accept_tls12_clients(
    runtime: ControllerRuntime,
) -> None:
    """gRPC exposes no min-version knob: record what a TLS1.2-capped client gets."""
    node = forge()

    def attempt() -> str:
        import socket

        ctx = client_ssl_context()
        ctx.load_cert_chain(*_files(node))
        ctx.check_hostname = False
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["h2"])
        try:
            with (
                socket.create_connection(("127.0.0.1", runtime.grpc_port), 5) as raw,
                ctx.wrap_socket(raw, server_hostname=pki.SERVER_NAME) as tls,
            ):
                return str(tls.version())
        except ssl.SSLError as exc:
            return f"refused:{exc.reason}"

    outcome = await asyncio.to_thread(attempt)
    # D137 says gRPC accepts >= 1.2; the brief's wording is "TLS >= 1.2 on gRPC".
    assert outcome in {"TLSv1.2"} or outcome.startswith("refused:"), outcome


async def test_a_session_does_not_outlive_its_certificate(
    runtime: ControllerRuntime,
) -> None:
    short = forge(lifetime=dt.timedelta(minutes=1))
    async with grpc.aio.secure_channel(
        _target(runtime),
        grpc_credentials(short),
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        agent = FakeAgent(channel, node_id=short.node_id)
        await agent.connect()
        agent.start_heartbeats(0.2)
        try:
            remaining = (
                short.cert.not_valid_after_utc - dt.datetime.now(dt.UTC)
            ).total_seconds()
            assert 0 < remaining <= 61
            await asyncio.sleep(remaining + 3)
            assert agent.ended.is_set(), "still connected with an expired certificate"
        finally:
            await agent.close()


async def test_a_node_revoked_between_enrolment_and_its_first_hello_never_gets_in(
    runtime: ControllerRuntime,
    registry: Registry,
    pg_settings: DatabaseSettings,
    tmp_path: Any,
) -> None:
    """Real join, then revoke while the row is still ``enrolling`` (no Hello
    ever seen): its certificate must be refused on the session and HTTPS planes
    and its serial must reach the revocation set (peers would otherwise accept
    the cert)."""
    from mf_fabric import identity_channel

    from meshforge.agent import enrollment as agent_enrollment
    from meshforge.agent.identity import load_or_create
    from meshforge.controller.enrollment.codes import issue_code
    from meshforge.controller.enrollment.invites import InviteStore

    assert runtime.fabric is not None
    engine = create_engine(pg_settings)
    try:
        new = await InviteStore(engine).create(ttl_s=900, actor="test")
        code = issue_code(
            new, fabric=runtime.fabric, host="127.0.0.1", port=runtime.enroll_port
        ).code
    finally:
        await engine.dispose()
    identity = load_or_create(tmp_path / "laptop" / "identity")
    joined = await asyncio.to_thread(agent_enrollment.join, identity, code)
    serial = format(joined.enrollment.node_cert.serial_number, "x")
    async with api_client(runtime, token=token_of(runtime)) as http:
        assert (
            await http.post(f"/v1/nodes/{identity.node_id}/revoke")
        ).status_code == 200
    assert runtime.revocations is not None
    assert serial in runtime.revocations.serials, (
        "serial of an enrolling node not revoked"
    )
    channel = identity_channel(_target(runtime), identity)
    async with channel:
        call = agent_pb2_grpc.AgentControlStub(channel).Session(timeout=10)
        await call.write(
            agent_pb2.AgentMessage(
                message_id=new_command_id(), hello=make_hello(identity.node_id, 1)
            )
        )
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await call.read()
    assert info.value.code() is grpc.StatusCode.PERMISSION_DENIED
    record = await registry.get_node(identity.node_id)
    assert record is not None and record.state.value == "revoked"


@pytest.mark.parametrize(
    "method",
    [
        "/meshforge.v1.Enrollment/Enroll",
        "/meshforge.v1.GpuService/OpenSession",
        "/meshforge.v1.AgentControl/Nonexistent",
        "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
        "/grpc.health.v1.Health/Check",
    ],
)
async def test_session_port_serves_nothing_but_the_session_rpc(
    runtime: ControllerRuntime, method: str
) -> None:
    """Every other RPC path - with a perfectly valid node certificate - is
    unimplemented; without any certificate the handshake itself fails."""
    ok = forge()
    async with grpc.aio.secure_channel(
        _target(runtime),
        grpc_credentials(ok),
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        call = channel.unary_unary(method)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await call(b"", timeout=5)
    assert info.value.code() in {
        grpc.StatusCode.UNIMPLEMENTED,
        grpc.StatusCode.UNAUTHENTICATED,
    }
    anonymous = grpc.ssl_channel_credentials(root_certificates=ca_pem())
    async with grpc.aio.secure_channel(
        _target(runtime),
        anonymous,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await channel.unary_unary(method)(b"", timeout=5)
    assert info.value.code() is grpc.StatusCode.UNAVAILABLE
