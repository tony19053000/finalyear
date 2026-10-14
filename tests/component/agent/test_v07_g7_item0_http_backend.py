"""V0.7 G7 item 0 (D165 (1)): a real HTTPS fetch through the agent's
transport really opens its connection through :class:`CancelSafeBackend`.

``cancel_safe_transport`` swaps httpcore's private ``_pool._network_backend``.
The unit pin (``tests/unit/agent/test_v07_g6_exitlist_http_connect.py``) only
checks that the attribute holds our backend; if an httpcore upgrade renamed
the attribute the pool reads, the assignment would set a dead attribute, that
check would still pass, and the cancel leak would silently return. Here the
backend's ``connect_tcp`` and ``start_tls`` are counted on a real TLS
request: zero calls = the pool no longer uses it = this test fails loudly.
"""

from __future__ import annotations

import asyncio
import datetime
import ssl
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from meshforge.agent import http_connect
from meshforge.agent.http_tls import AgentHttpTls

pytestmark = [pytest.mark.component, pytest.mark.timeout(60)]


def _server_and_client_contexts(tmp: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    (tmp / "c.pem").write_bytes(cert_pem)
    (tmp / "k.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(tmp / "c.pem", tmp / "k.pem")
    client = ssl.create_default_context(cadata=cert_pem.decode())
    return server, client


@pytest.mark.asyncio
async def test_a_real_https_fetch_opens_its_connection_through_the_cancel_safe_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"connect_tcp": 0, "start_tls": 0}
    real_connect = http_connect.CancelSafeBackend.connect_tcp
    real_tls = http_connect._CancelSafeStream.start_tls

    async def counting_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["connect_tcp"] += 1
        return await real_connect(self, *args, **kwargs)

    async def counting_tls(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["start_tls"] += 1
        return await real_tls(self, *args, **kwargs)

    monkeypatch.setattr(http_connect.CancelSafeBackend, "connect_tcp", counting_connect)
    monkeypatch.setattr(http_connect._CancelSafeStream, "start_tls", counting_tls)

    server_ctx, client_ctx = _server_and_client_contexts(tmp_path)

    async def answer(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        # The production path: AgentHttpTls.transport() -> cancel_safe_transport.
        transport = AgentHttpTls(context=client_ctx).transport()
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            response = await client.get(
                f"https://127.0.0.1:{port}/",
                extensions={"sni_hostname": "localhost"},
            )
            assert response.status_code == 200
            assert response.text == "ok"
            pool = transport._pool
            assert isinstance(pool, httpcore.AsyncConnectionPool)
            assert len(pool.connections) <= 1
    finally:
        server.close()
        await server.wait_closed()
    # One connection, opened and upgraded to TLS by our backend - not httpcore's.
    assert calls == {"connect_tcp": 1, "start_tls": 1}
