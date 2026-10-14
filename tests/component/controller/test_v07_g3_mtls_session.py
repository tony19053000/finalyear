"""V0.7 G3 deliverable 3 (0.7.5, D137): mutual TLS on the agent session port.

Real PostgreSQL, the real ``ControllerRuntime`` in ``mtls`` mode, real gRPC
(BoringSSL) clients:

* no client certificate, one from another CA, an expired one -> the handshake
  fails; no stream, nothing persisted;
* ``Hello.node_id`` other than the certificate's -> ``UNAUTHENTICATED`` /
  ``node_id_mismatch``, nothing persisted;
* a node's first certificate is recorded at its first Hello; an older
  certificate of a re-keyed node is ``cert_superseded``;
* TLS 1.3 is what the server negotiates (a Python client and a gRPC client,
  the latter read from the ServerHello on the wire);
* ``mtls`` without a fabric CA refuses to start; ``insecure-dev`` serves
  plaintext on loopback and says so in its log.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
from collections.abc import AsyncIterator

import grpc
import pytest
import pytest_asyncio
import structlog
from mf_controller_harness import make_settings
from mf_fabric import (
    NodeChannels,
    ca_pem,
    client_ssl_context,
    fabric_ca,
    grpc_channel,
    node_material,
)
from mf_session_client import FakeAgent, make_hello

from meshforge.common import pki
from meshforge.common.errors import ConfigError
from meshforge.common.ids import new_command_id, new_fabric_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]


def _target(rt: ControllerRuntime) -> str:
    return f"127.0.0.1:{rt.grpc_port}"


async def _hello_status(
    channel: grpc.aio.Channel, node_id: str
) -> tuple[grpc.StatusCode | None, dict[str, str]]:
    """Open a Session, send a Hello for ``node_id``; the final status + trailers."""
    call = agent_pb2_grpc.AgentControlStub(channel).Session(timeout=10)
    try:
        await call.write(
            agent_pb2.AgentMessage(
                message_id=new_command_id(), hello=make_hello(node_id, 1)
            )
        )
        reply = await call.read()
        if (
            reply is not grpc.aio.EOF
            and reply.WhichOneof("payload") == "hello_ack"
            and reply.hello_ack.accepted
        ):
            return grpc.StatusCode.OK, {}
        code = await call.code()
        trailing = await call.trailing_metadata()
    except grpc.aio.AioRpcError as exc:
        code, trailing = exc.code(), exc.trailing_metadata()
    except asyncio.InvalidStateError:
        code, trailing = await call.code(), await call.trailing_metadata()
    finally:
        call.cancel()
    return code, {k: v for k, v in (trailing or ()) if isinstance(v, str)}


# ------------------------------------------------------------- handshake


async def test_no_client_cert_is_refused_at_the_handshake(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    node = str(new_node_id())
    credentials = grpc.ssl_channel_credentials(root_certificates=ca_pem())
    async with grpc.aio.secure_channel(
        _target(runtime),
        credentials,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        code, _ = await _hello_status(channel, node)
    assert code is grpc.StatusCode.UNAVAILABLE
    assert await registry.get_node(node) is None


async def test_plaintext_client_is_refused(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    node = str(new_node_id())
    async with grpc.aio.insecure_channel(_target(runtime)) as channel:
        code, _ = await _hello_status(channel, node)
    assert code is grpc.StatusCode.UNAVAILABLE
    assert await registry.get_node(node) is None


async def test_cert_from_another_ca_is_refused(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    other = pki.create_ca(new_fabric_id())
    node = str(new_node_id())
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        other,
        pki.build_csr(key, fabric_id=other.fabric_id, node_id=node),
        node_id=node,
        fabric_id=other.fabric_id,
    )
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca_pem(),
        private_key=pki.key_to_pem(key),
        certificate_chain=pki.cert_to_pem(cert),
    )
    async with grpc.aio.secure_channel(
        _target(runtime),
        credentials,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        code, _ = await _hello_status(channel, node)
    assert code is grpc.StatusCode.UNAVAILABLE
    assert await registry.get_node(node) is None


@pytest_asyncio.fixture
async def past_runtime(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[tuple[ControllerRuntime, pki.CertificateAuthority]]:
    """A controller whose CA dates from yesterday, so it can issue an already
    expired node certificate for the test."""
    settings = make_settings(pg_settings, fabric=False)
    fabric = create_fabric(
        settings.ca_dir, now=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    )
    async with ControllerRuntime.start(settings) as rt:
        yield rt, fabric.authority


async def test_expired_cert_is_refused_at_the_handshake(
    past_runtime: tuple[ControllerRuntime, pki.CertificateAuthority],
    registry: Registry,
) -> None:
    runtime, ca = past_runtime
    node = str(new_node_id())
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node),
        node_id=node,
        fabric_id=ca.fabric_id,
        now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
        lifetime=dt.timedelta(minutes=30),
    )
    credentials = grpc.ssl_channel_credentials(
        root_certificates=pki.cert_to_pem(ca.cert),
        private_key=pki.key_to_pem(key),
        certificate_chain=pki.cert_to_pem(cert),
    )
    async with grpc.aio.secure_channel(
        _target(runtime),
        credentials,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        code, _ = await _hello_status(channel, node)
    assert code is grpc.StatusCode.UNAVAILABLE
    assert await registry.get_node(node) is None


# ------------------------------------------------------------ identity


async def test_hello_for_another_node_is_unauthenticated(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    mine, victim = str(new_node_id()), str(new_node_id())
    with structlog.testing.capture_logs() as logs:
        async with grpc_channel(_target(runtime), mine) as channel:
            code, trailing = await _hello_status(channel, victim)
    assert code is grpc.StatusCode.UNAUTHENTICATED
    assert trailing.get("meshforge-reason") == "node_id_mismatch"
    assert await registry.get_node(victim) is None
    assert await registry.get_node(mine) is None
    assert any(e["event"] == "session.node_id_mismatch" for e in logs)


async def test_victim_session_is_not_superseded_by_an_impostor(
    runtime: ControllerRuntime,
) -> None:
    """CG-S2: knowing a node's id is no longer enough to take its session."""
    async with NodeChannels(_target(runtime)) as pool:
        victim = FakeAgent(pool)
        await victim.connect()
        try:
            async with grpc_channel(_target(runtime), str(new_node_id())) as other:
                code, _ = await _hello_status(other, victim.node_id)
            assert code is grpc.StatusCode.UNAUTHENTICATED
            await asyncio.sleep(0.3)
            assert not victim.ended.is_set(), (victim.end_code, victim.end_details)
            assert runtime.manager is not None
            live = runtime.manager.get(victim.node_id)
            assert live is not None and live.session_id == victim.session_id
        finally:
            await victim.close()


async def test_first_hello_records_the_certificate(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    async with NodeChannels(_target(runtime)) as pool:
        agent = FakeAgent(pool)
        await agent.connect()
        await agent.close()
    record = await registry.get_node(agent.node_id)
    assert record is not None
    _, cert_pem = node_material(agent.node_id)
    cert = pki.cert_from_pem(cert_pem)
    assert record.cert_serial == format(cert.serial_number, "x")
    assert record.cert_fingerprint == pki.cert_fingerprint(cert)
    assert record.cert_not_after == cert.not_valid_after_utc


async def test_an_older_certificate_of_a_rekeyed_node_is_refused(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    node = str(new_node_id())
    async with grpc_channel(_target(runtime), node) as first:
        code, _ = await _hello_status(first, node)
    assert code is grpc.StatusCode.OK
    # A second, equally CA-valid certificate for the same node (e.g. the old
    # laptop after `invite --replace` re-keyed the node) is not its identity.
    ca = fabric_ca()
    key = pki.generate_key()
    stale = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node),
        node_id=node,
        fabric_id=ca.fabric_id,
    )
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca_pem(),
        private_key=pki.key_to_pem(key),
        certificate_chain=pki.cert_to_pem(stale),
    )
    async with grpc.aio.secure_channel(
        _target(runtime),
        credentials,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        call = agent_pb2_grpc.AgentControlStub(channel).Session(timeout=10)
        await call.write(
            agent_pb2.AgentMessage(
                message_id=new_command_id(), hello=make_hello(node, 2)
            )
        )
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await call.read()
    assert info.value.code() is grpc.StatusCode.UNAUTHENTICATED
    assert dict(info.value.trailing_metadata() or ())["meshforge-reason"] == (
        "cert_superseded"
    )
    record = await registry.get_node(node)
    assert record is not None and record.session_epoch == 1  # nothing moved


# ---------------------------------------------------------------- TLS 1.3


async def test_python_client_negotiates_tls13_with_alpn_h2(
    runtime: ControllerRuntime,
) -> None:
    def handshake() -> tuple[str | None, str | None]:
        ctx = client_ssl_context(node_id=str(new_node_id()))
        ctx.check_hostname = False
        ctx.set_alpn_protocols(["h2"])
        with (
            socket.create_connection(("127.0.0.1", runtime.grpc_port), 5) as raw,
            ctx.wrap_socket(raw, server_hostname=pki.SERVER_NAME) as tls,
        ):
            return tls.version(), tls.selected_alpn_protocol()

    version, alpn = await asyncio.to_thread(handshake)
    assert version == "TLSv1.3"
    assert alpn == "h2"


def _server_hello_version(data: bytes) -> int | None:
    """The TLS version a ServerHello selected (supported_versions or legacy)."""
    if len(data) < 9 or data[0] != 0x16 or data[5] != 0x02:
        return None
    body = data[9:]
    legacy = int.from_bytes(body[0:2], "big")
    pos = 2 + 32
    pos += 1 + body[pos]  # session id
    pos += 2 + 1  # cipher suite, compression
    end = pos + 2 + int.from_bytes(body[pos : pos + 2], "big")
    pos += 2
    while pos + 4 <= end:
        kind = int.from_bytes(body[pos : pos + 2], "big")
        size = int.from_bytes(body[pos + 2 : pos + 4], "big")
        if kind == 0x002B:  # supported_versions
            return int.from_bytes(body[pos + 4 : pos + 6], "big")
        pos += 4 + size
    return legacy


async def test_grpc_client_and_server_negotiate_tls13_on_the_wire(
    runtime: ControllerRuntime,
) -> None:
    """D137: grpcio exposes no minimum-version knob; this proves what BoringSSL
    picks between our client and our server, from the ServerHello itself."""
    first_server_bytes: list[bytes] = []

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_r, up_w = await asyncio.open_connection("127.0.0.1", runtime.grpc_port)

        async def pump(
            src: asyncio.StreamReader, dst: asyncio.StreamWriter, record: bool
        ) -> None:
            try:
                while data := await src.read(65536):
                    if record and not first_server_bytes:
                        first_server_bytes.append(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pump(reader, up_w, False), pump(up_r, writer, True))

    server = await asyncio.start_server(relay, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        node = str(new_node_id())
        async with grpc_channel(f"127.0.0.1:{port}", node) as channel:
            code, _ = await _hello_status(channel, node)
        assert code is grpc.StatusCode.OK
    finally:
        server.close()
    assert first_server_bytes
    assert _server_hello_version(first_server_bytes[0]) == 0x0304  # TLS 1.3


# -------------------------------------------------------------- modes


async def test_mtls_without_a_fabric_ca_refuses_to_start(
    pg_settings: DatabaseSettings,
) -> None:
    settings = make_settings(pg_settings, fabric=False)
    with pytest.raises(ConfigError, match="fabric create"):
        async with ControllerRuntime.start(settings):
            pass


async def test_insecure_dev_serves_plaintext_on_loopback_and_says_so(
    pg_settings: DatabaseSettings, registry: Registry
) -> None:
    from meshforge.controller.settings import ControllerSettings

    base = make_settings(pg_settings, fabric=False)
    settings = ControllerSettings(
        security={"mode": "insecure-dev"},
        controller=base.controller,
        database=base.database,
    )
    with structlog.testing.capture_logs() as logs:
        async with ControllerRuntime.start(settings) as rt:
            assert rt.fabric is None and rt.server_tls is None
            node = str(new_node_id())
            async with grpc.aio.insecure_channel(_target(rt)) as channel:
                code, _ = await _hello_status(channel, node)
            assert code is grpc.StatusCode.OK
    banners = [e for e in logs if e["event"] == "security.insecure_dev"]
    assert banners and banners[0]["log_level"] == "warning"
    assert "SECURITY IS OFF" in banners[0]["banner"]
