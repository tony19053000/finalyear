"""V0.7 G3 deliverable 1 (D149 carry-over): renew the server certificate under a
**live** controller - new handshakes see the new serial on every TLS listener,
connections opened before the renewal keep working, nothing restarts.

Real PostgreSQL + the real ``ControllerRuntime``. The certificate is made
"almost due" before the start (``server_cert_days = 1``, issued 15 h 57 min
ago, renewal at 16 h), then the renewer's wall clock moves 4 minutes on.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import grpc
import pytest
import pytest_asyncio
from cryptography import x509

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import make_settings

from meshforge.common import pki
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.fabric_ca import create_fabric, ensure_server_cert
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

DAY = timedelta(days=1)


def _serial(port: int) -> int:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # only reading which certificate is served
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        ctx.wrap_socket(raw, server_hostname="meshforge-controller") as tls,
    ):
        der = tls.getpeercert(binary_form=True)
        assert der is not None
        return x509.load_der_x509_certificate(der).serial_number


@pytest_asyncio.fixture
async def rt(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(pg_settings, fabric=False, tls={"server_cert_days": 1})
    issued_at = datetime.now(UTC) - (DAY * 2 / 3 - timedelta(minutes=3))
    fabric = create_fabric(settings.ca_dir, now=issued_at - timedelta(hours=1))
    ensure_server_cert(
        settings.tls_dir,
        fabric,
        hosts=settings.server_cert_hosts,
        now=issued_at,
        lifetime=DAY,
    )
    async with ControllerRuntime.start(settings) as runtime:
        yield runtime


async def test_renewal_under_a_live_controller(rt: ControllerRuntime) -> None:
    assert rt.server_tls is not None and rt.fabric is not None
    first = rt.server_tls.current.cert.serial_number
    assert rt.server_cert is not None and rt.server_cert.action.value == "kept"
    assert await asyncio.to_thread(_serial, rt.enroll_port) == first

    creds = grpc.ssl_channel_credentials(pki.cert_to_pem(rt.fabric.cert))
    options = [("grpc.ssl_target_name_override", pki.SERVER_NAME)]
    async with grpc.aio.secure_channel(
        f"127.0.0.1:{rt.enroll_port}", creds, options=options
    ) as before:
        stub = enroll_pb2_grpc.EnrollmentStub(before)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Enroll(enroll_pb2.EnrollRequest(join_token=b"short"))
        assert info.value.code() is grpc.StatusCode.INVALID_ARGUMENT  # it answers

        later = datetime.now(UTC) + timedelta(minutes=4)
        rt.server_tls._now = lambda: later
        assert await rt.server_tls.check() is True
        renewed = rt.server_tls.current.cert.serial_number
        assert renewed != first
        assert await asyncio.to_thread(_serial, rt.enroll_port) == renewed

        # the channel opened before the renewal still talks on its connection
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Enroll(enroll_pb2.EnrollRequest(join_token=b"short"))
        assert info.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    # and a new client that verifies the chain is happy with the renewed one
    async with grpc.aio.secure_channel(
        f"127.0.0.1:{rt.enroll_port}", creds, options=options
    ) as after:
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await enroll_pb2_grpc.EnrollmentStub(after).Enroll(
                enroll_pb2.EnrollRequest(join_token=b"short")
            )
        assert info.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    on_disk = pki.cert_from_pem((rt.settings.tls_dir / "server.crt").read_bytes())
    assert on_disk.serial_number == renewed
