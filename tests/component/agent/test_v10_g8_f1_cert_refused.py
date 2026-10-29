"""V1.0 G8 F1: an agent whose node certificate the server refuses gets a typed
"certificate refused" error, not a network fault - against real TLS servers.

The server is a real asyncio HTTPS server using the controller's own
:meth:`ServerTls.ssl_context` (TLS 1.3, ``optional`` client certificates - the
controller's HTTP port - and ``required``, like an agent's peer server). With
TLS 1.3 asyncio usually drops the ``unknown_ca`` / ``certificate_expired``
alert and the client sees EOF / EPIPE / reset right after its handshake
(``docs/briefs/progress/V1.0-G8-flakes.md``). The client is the agent's
production transport (:meth:`AgentHttpTls.transport`) and, end to end, the
agent's real :func:`~meshforge.agent.execution.fetch.fetch_bundle`.

A blocking ``ssl`` server, which *does* send the alert, covers the alert path;
a closed port covers "no handshake at all" (still a plain network error).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from meshforge.agent.execution.fetch import fetch_bundle
from meshforge.agent.http_refusal import transport_failure
from meshforge.agent.http_tls import AgentHttpTls
from meshforge.common import pki
from meshforge.common.errors import MeshForgeError
from meshforge.common.fabric_tls import client_context
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.tls_refusal import (
    CERT_REFUSED_HINT,
    CLOSED_AFTER_HANDSHAKE,
    find_refusal,
)
from meshforge.controller.fabric_ca import create_fabric, ensure_server_cert
from meshforge.controller.server_tls import ClientCerts, ServerTls

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]

PROBES = 15
"""Requests per refused case: the refusal shows up as EOF, EPIPE or a reset
depending on timing; every one of them must be recognised."""

_YESTERDAY = datetime.now(UTC) - timedelta(days=1)


@dataclass(frozen=True)
class Pki:
    server_tls: ServerTls
    ca_cert_pem: bytes
    tmp: Path
    fabric: pki.CertificateAuthority

    def agent_context(self, name: str) -> ssl.SSLContext:
        """The agent's real client context, presenting ``name``'s certificate."""
        return client_context(
            self.fabric.cert,
            cert_file=self.tmp / f"{name}.crt",
            key_file=self.tmp / f"{name}.key",
        )


def _node(tmp: Path, ca: pki.CertificateAuthority, name: str, **issue: object) -> None:
    node_id = str(new_node_id())
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node_id),
        node_id=node_id,
        fabric_id=ca.fabric_id,
        **issue,  # type: ignore[arg-type]
    )
    (tmp / f"{name}.key").write_bytes(pki.key_to_pem(key))
    (tmp / f"{name}.crt").write_bytes(pki.cert_to_pem(cert))


@pytest.fixture(scope="module")
def material(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    tmp = tmp_path_factory.mktemp("f1")
    fabric = create_fabric(tmp / "controller" / "ca", now=_YESTERDAY)
    first = ensure_server_cert(tmp / "controller" / "tls", fabric, hosts=["127.0.0.1"])
    holder = ServerTls(
        fabric, first, hosts=["127.0.0.1"], lifetime=pki.SERVER_CERT_LIFETIME
    )
    foreign = pki.create_ca(new_fabric_id())
    _node(tmp, fabric.authority, "good")
    _node(tmp, foreign, "foreign")
    # Issued two hours ago for one minute: expired, but from the right CA.
    _node(
        tmp,
        fabric.authority,
        "expired",
        now=datetime.now(UTC) - timedelta(hours=2),
        lifetime=timedelta(minutes=1),
    )
    return Pki(
        server_tls=holder,
        ca_cert_pem=pki.cert_to_pem(fabric.cert),
        tmp=tmp,
        fabric=fabric.authority,
    )


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:  # keep-alive: answer every request on the connection
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
        pass
    finally:
        writer.close()


@pytest_asyncio.fixture(params=["optional", "required"])
async def server(request: pytest.FixtureRequest, material: Pki) -> AsyncIterator[str]:
    mode: ClientCerts = request.param
    context = material.server_tls.ssl_context(client_certs=mode)
    srv = await asyncio.start_server(_answer, "127.0.0.1", 0, ssl=context)
    port = srv.sockets[0].getsockname()[1]
    try:
        yield f"https://127.0.0.1:{port}"
    finally:
        srv.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), 5)


async def _get(context: ssl.SSLContext, url: str) -> httpx.Response:
    transport = AgentHttpTls(context=context).transport()
    async with httpx.AsyncClient(transport=transport, timeout=10) as client:
        return await client.get(f"{url}/v1/ping")


@pytest.mark.asyncio
async def test_the_right_certificate_is_served_on_a_kept_alive_connection(
    material: Pki, server: str
) -> None:
    transport = AgentHttpTls(context=material.agent_context("good")).transport()
    async with httpx.AsyncClient(transport=transport, timeout=10) as client:
        for _ in range(3):  # the watcher must not disturb keep-alive
            response = await client.get(f"{server}/v1/ping")
            assert response.status_code == 200
            assert response.text == "ok"


@pytest.mark.parametrize("cert", ["foreign", "expired"])
@pytest.mark.asyncio
async def test_a_refused_certificate_is_a_typed_refusal(
    material: Pki, server: str, cert: str
) -> None:
    seen: set[str] = set()
    for _ in range(PROBES):
        with pytest.raises(httpx.TransportError) as caught:
            await _get(material.agent_context(cert), server)
        refusal = find_refusal(caught.value)
        assert refusal is not None, repr(caught.value)
        assert refusal.server == "the controller"
        seen.add(type(caught.value).__name__)
        text = transport_failure(caught.value)
        assert "refused this laptop's certificate" in text
        assert CERT_REFUSED_HINT in text
    # httpx's own exception types are kept (callers' except clauses unchanged)
    assert seen <= {"RemoteProtocolError", "ReadError", "WriteError"}, seen


@pytest.mark.parametrize("cert", ["foreign", "expired"])
@pytest.mark.asyncio
async def test_the_agents_bundle_fetch_says_why(
    material: Pki, server: str, cert: str, tmp_path: Path
) -> None:
    transport = AgentHttpTls(context=material.agent_context(cert)).transport()
    with pytest.raises(MeshForgeError) as caught:
        await fetch_bundle(
            server,
            Digest.from_bytes(bytes(32)),
            "token",
            tmp_path / "bundle",
            transport=transport,
            timeout_s=10,
        )
    message = caught.value.message
    assert message.startswith("bundle fetch failed: the controller ")
    assert "wrong fabric or expired certificate?" in message
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))


@pytest.mark.asyncio
async def test_a_closed_port_is_still_a_network_error(
    material: Pki, tmp_path: Path
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again before the request
    url = f"https://127.0.0.1:{port}"
    with pytest.raises(httpx.ConnectError) as caught:
        await _get(material.agent_context("good"), url)
    assert find_refusal(caught.value) is None
    assert transport_failure(caught.value) == "ConnectError"
    with pytest.raises(MeshForgeError) as fetch_caught:
        await fetch_bundle(
            url,
            Digest.from_bytes(bytes(32)),
            "token",
            tmp_path / "bundle",
            transport=AgentHttpTls(
                context=material.agent_context("foreign")
            ).transport(),
            timeout_s=10,
        )
    assert fetch_caught.value.message == "bundle fetch failed: ConnectError"


@contextlib.contextmanager
def _alerting_server(material: Pki) -> Iterator[str]:
    """A blocking TLS server for one connection: its failed handshake *does*
    send the alert."""
    context = material.server_tls.ssl_context(client_certs="required")
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(10)

    def serve() -> None:
        try:
            raw, _ = listener.accept()
        except OSError:
            return
        with raw, contextlib.suppress(OSError):
            context.wrap_socket(raw, server_side=True).close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        thread.join(15)
        listener.close()


@pytest.mark.parametrize(
    ("cert", "alert"),
    [
        ("foreign", "TLSV1_ALERT_UNKNOWN_CA"),
        ("expired", "SSLV3_ALERT_CERTIFICATE_EXPIRED"),
    ],
)
@pytest.mark.asyncio
async def test_an_alert_that_arrives_names_its_reason(
    material: Pki, cert: str, alert: str
) -> None:
    with (
        _alerting_server(material) as url,
        pytest.raises(httpx.TransportError) as caught,
    ):
        await _get(material.agent_context(cert), url)
    refusal = find_refusal(caught.value)
    assert refusal is not None, repr(caught.value)
    # The alert normally wins; a reset racing it is still a refusal.
    assert refusal.reason in {alert, CLOSED_AFTER_HANDSHAKE}
