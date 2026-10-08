"""V0.7 G3 deliverable 1 (D149 carry-over): the server certificate is renewed
while the controller runs and new handshakes use it without a restart.

Real CA, real files in a temporary ``tls/`` folder, real TLS servers (Python
``ssl`` on asyncio, and grpc.aio with dynamic credentials) on loopback. The
wall clock is injected so "two thirds of the certificate's life" is reached
without waiting.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import grpc
import pytest
import pytest_asyncio
from cryptography import x509

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.fabric_ca import (
    FabricCa,
    create_fabric,
    ensure_server_cert,
)
from meshforge.controller.server_tls import ServerTls

pytestmark = pytest.mark.unit

LIFE = timedelta(days=3)


class Wall:
    """Starts 70 % of a certificate life in the past: the first certificate is
    valid now but already due; :meth:`catch_up` moves to the real now, so the
    renewed certificate is valid for real TLS stacks too."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC) - LIFE * 0.7
        self.frozen = True

    def catch_up(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def wall() -> Wall:
    return Wall()


@pytest.fixture
def fabric(tmp_path: Path, wall: Wall) -> FabricCa:
    return create_fabric(tmp_path / "controller" / "ca", now=wall())


@pytest.fixture
def holder(tmp_path: Path, fabric: FabricCa, wall: Wall) -> ServerTls:
    tls_dir = tmp_path / "controller" / "tls"
    first = ensure_server_cert(
        tls_dir, fabric, hosts=["127.0.0.1"], now=wall(), lifetime=LIFE
    )
    return ServerTls(fabric, first, hosts=["127.0.0.1"], lifetime=LIFE, now=wall)


def _client_context(fabric: FabricCa, *, verify: bool = True) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    if verify:
        ctx.load_verify_locations(cadata=pki.cert_to_pem(fabric.cert).decode())
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _serial_seen(port: int, fabric: FabricCa, alpn: list[str] | None = None) -> int:
    ctx = _client_context(fabric, verify=alpn is None)
    if alpn:
        ctx.set_alpn_protocols(alpn)
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
    ):
        der = tls.getpeercert(binary_form=True)
        assert der is not None
        return x509.load_der_x509_certificate(der).serial_number


@pytest_asyncio.fixture
async def echo_server(holder: ServerTls) -> AsyncIterator[int]:
    ctx = holder.ssl_context(client_certs="optional")

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.readline():
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0, ssl=ctx)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_not_due_is_a_no_op(holder: ServerTls) -> None:
    serial = holder.current.cert.serial_number
    assert await holder.check() is False
    assert holder.current.cert.serial_number == serial
    assert holder.generation == 0 and holder.renewals == 0


@pytest.mark.asyncio
async def test_ssl_context_new_handshakes_see_the_renewed_cert_old_ones_keep_working(
    holder: ServerTls, fabric: FabricCa, wall: Wall, echo_server: int
) -> None:
    port = echo_server
    first = holder.current.cert.serial_number
    assert await asyncio.to_thread(_serial_seen, port, fabric) == first
    # A connection that stays open across the renewal.
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=_client_context(fabric), server_hostname="127.0.0.1"
    )
    try:
        writer.write(b"before\n")
        assert await reader.readline() == b"before\n"
        wall.catch_up()  # the first cert is now past 2/3 of its life
        assert await holder.check() is True
        renewed = holder.current.cert.serial_number
        assert renewed != first
        assert holder.generation == 1 and holder.renewals == 1
        assert await asyncio.to_thread(_serial_seen, port, fabric) == renewed
        writer.write(b"after\n")  # the old connection is untouched
        assert await reader.readline() == b"after\n"
    finally:
        writer.close()
    # The files on disk are the new pair too (a restart serves the same one).
    on_disk = pki.cert_from_pem((holder.current.path / "server.crt").read_bytes())
    assert on_disk.serial_number == renewed


@pytest.mark.asyncio
async def test_ssl_context_is_tls13_only(holder: ServerTls, echo_server: int) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises(ssl.SSLError):
        await asyncio.to_thread(_handshake, echo_server, ctx)


def _handshake(port: int, ctx: ssl.SSLContext) -> str | None:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        ctx.wrap_socket(raw) as tls,
    ):
        return tls.version()


class _Echo(enroll_pb2_grpc.EnrollmentServicer):
    async def Enroll(  # noqa: N802 - gRPC method name
        self, request: enroll_pb2.EnrollRequest, context: object
    ) -> enroll_pb2.EnrollResponse:
        return enroll_pb2.EnrollResponse(node_id="echo")


@pytest.mark.asyncio
async def _grpc_server(
    holder: ServerTls, *, require: bool
) -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()
    enroll_pb2_grpc.add_EnrollmentServicer_to_server(_Echo(), server)
    port = server.add_secure_port(
        "127.0.0.1:0", holder.grpc_credentials(require_client_auth=require)
    )
    await server.start()
    return server, port


def _node_creds(holder: ServerTls) -> grpc.ChannelCredentials:
    ca = holder._fabric.authority
    key = pki.generate_key()
    node = str(new_node_id())
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node),
        node_id=node,
        fabric_id=ca.fabric_id,
    )
    return grpc.ssl_channel_credentials(
        root_certificates=holder.ca_pem,
        private_key=pki.key_to_pem(key),
        certificate_chain=pki.cert_to_pem(cert),
    )


@pytest.mark.parametrize("require", [False, True])
@pytest.mark.asyncio
async def test_grpc_dynamic_credentials_follow_the_renewal(
    holder: ServerTls, fabric: FabricCa, wall: Wall, require: bool
) -> None:
    server, port = await _grpc_server(holder, require=require)
    options = [("grpc.ssl_target_name_override", pki.SERVER_NAME)]
    try:
        first = holder.current.cert.serial_number
        assert await asyncio.to_thread(_serial_seen, port, fabric, ["h2"]) == first
        async with grpc.aio.secure_channel(
            f"127.0.0.1:{port}", _node_creds(holder), options=options
        ) as channel:
            stub = enroll_pb2_grpc.EnrollmentStub(channel)
            assert (await stub.Enroll(enroll_pb2.EnrollRequest())).node_id == "echo"
            wall.catch_up()
            assert await holder.check() is True
            renewed = holder.current.cert.serial_number
            seen = await asyncio.to_thread(_serial_seen, port, fabric, ["h2"])
            assert seen == renewed
            # the open channel keeps its connection and its old handshake
            assert (await stub.Enroll(enroll_pb2.EnrollRequest())).node_id == "echo"
        # a brand-new client still verifies the renewed chain
        async with grpc.aio.secure_channel(
            f"127.0.0.1:{port}", _node_creds(holder), options=options
        ) as fresh:
            stub = enroll_pb2_grpc.EnrollmentStub(fresh)
            assert (await stub.Enroll(enroll_pb2.EnrollRequest())).node_id == "echo"
    finally:
        await server.stop(None)


@pytest.mark.asyncio
async def test_a_failed_check_keeps_serving_and_is_counted(
    holder: ServerTls, fabric: FabricCa, wall: Wall, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.controller import server_tls
    from meshforge.controller.fabric_ca import FabricCaError

    def broken(*args: object, **kwargs: object) -> object:
        raise FabricCaError("disk full")

    monkeypatch.setattr(server_tls, "ensure_server_cert", broken)
    first = holder.current.cert.serial_number
    wall.catch_up()
    assert await holder.check() is False
    assert holder.check_failures == 1
    assert holder.current.cert.serial_number == first
    assert holder.generation == 0


@pytest.mark.asyncio
async def test_run_checks_periodically(
    holder: ServerTls, wall: Wall, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real: Callable[[], object] = holder.check

    async def counting() -> bool:
        calls.append(1)
        return bool(await real())  # type: ignore[misc]

    monkeypatch.setattr(holder, "check", counting)
    task = asyncio.create_task(holder.run(0.02))
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 3


def test_settings_bound_the_interval() -> None:
    from meshforge.common.errors import ConfigError
    from meshforge.controller.settings import ControllerSection

    assert ControllerSection().tls.check_interval_s == 3600.0
    for bad in (0.5, 86_401):
        with pytest.raises((ConfigError, ValueError)):
            ControllerSection(tls={"check_interval_s": bad})


_ = new_fabric_id  # imported for symmetry with other PKI tests
