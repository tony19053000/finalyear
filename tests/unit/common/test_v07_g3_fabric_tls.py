"""V0.7 G3 deliverable 4 (D139): who may answer an HTTPS client of the fabric.

Real TLS 1.3 servers on loopback (asyncio + Python ``ssl``), real ``httpx``
clients (async and sync) with :func:`client_context`:

* the controller check: a server certificate with the ``meshforge-controller``
  name passes from any dialled address; an enrolled node's certificate (same
  CA, ``serverAuth``) and another CA's server are refused - and the server
  never receives a request byte (so never a token);
* the peer check (``sni_hostname`` = :func:`peer_server_name`): only the node
  the controller named passes; another node of the same fabric is refused;
* client certificates: a server that requires one gets the node certificate.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from meshforge.common import pki
from meshforge.common.fabric_tls import (
    client_context,
    expected_identity,
    peer_server_name,
)
from meshforge.common.ids import new_fabric_id, new_node_id

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id())


def _node(ca: pki.CertificateAuthority) -> tuple[str, pki.IssuedCert]:
    node = str(new_node_id())
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node),
        node_id=node,
        fabric_id=ca.fabric_id,
    )
    return node, pki.IssuedCert(key=key, cert=cert)


def _files(tmp: Path, name: str, issued: pki.IssuedCert) -> tuple[Path, Path]:
    cert, key = tmp / f"{name}.crt", tmp / f"{name}.key"
    cert.write_bytes(pki.cert_to_pem(issued.cert))
    key.write_bytes(pki.key_to_pem(issued.key))
    return cert, key


class _Server:
    def __init__(self) -> None:
        self.received = 0
        self.client_certs: list[bytes | None] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self, ctx: ssl.SSLContext) -> None:
        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            ssl_object = writer.get_extra_info("ssl_object")
            self.client_certs.append(
                ssl_object.getpeercert(binary_form=True) if ssl_object else None
            )
            try:
                data = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
                writer.close()
                return
            self.received += len(data)
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _server_ctx(
    tmp: Path, issued: pki.IssuedCert, ca: pki.CertificateAuthority, *, require: bool
) -> ssl.SSLContext:
    cert, key = _files(tmp, f"srv{id(issued)}", issued)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, key)
    if require:
        ctx.load_verify_locations(cadata=pki.cert_to_pem(ca.cert).decode())
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


@pytest_asyncio.fixture
async def servers() -> AsyncIterator[list[_Server]]:
    made: list[_Server] = []
    yield made
    for server in made:
        await server.close()


async def _serve(servers: list[_Server], ctx: ssl.SSLContext) -> _Server:
    server = _Server()
    await server.start(ctx)
    servers.append(server)
    return server


@pytest.mark.asyncio
async def test_controller_check(
    ca: pki.CertificateAuthority, tmp_path: Path, servers: list[_Server]
) -> None:
    controller = pki.issue_server_cert(ca, hosts=["10.9.9.9"])  # not 127.0.0.1
    _, node = _node(ca)
    other_ca = pki.create_ca(new_fabric_id())
    foreign = pki.issue_server_cert(other_ca, hosts=["127.0.0.1"])
    good = await _serve(servers, _server_ctx(tmp_path, controller, ca, require=False))
    rogue = await _serve(servers, _server_ctx(tmp_path, node, ca, require=False))
    alien = await _serve(servers, _server_ctx(tmp_path, foreign, ca, require=False))
    async with httpx.AsyncClient(verify=client_context(ca.cert)) as client:
        ok = await client.get(f"https://127.0.0.1:{good.port}/")
        assert ok.status_code == 200 and ok.text == "ok"
        for bad in (rogue, alien):
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://127.0.0.1:{bad.port}/")
    await asyncio.sleep(0.05)
    assert good.received > 0
    assert rogue.received == 0 and alien.received == 0  # not a byte sent


def test_controller_check_sync(ca: pki.CertificateAuthority, tmp_path: Path) -> None:
    """The CLI's synchronous client goes through ``sslsocket_class``."""

    async def run() -> tuple[int, int]:
        controller = pki.issue_server_cert(ca, hosts=["127.0.0.1"])
        _, node = _node(ca)
        made: list[_Server] = []
        good = await _serve(made, _server_ctx(tmp_path, controller, ca, require=False))
        rogue = await _serve(made, _server_ctx(tmp_path, node, ca, require=False))

        def calls() -> int:
            with httpx.Client(verify=client_context(ca.cert)) as client:
                assert client.get(f"https://127.0.0.1:{good.port}/").text == "ok"
                with pytest.raises(httpx.ConnectError):
                    client.get(f"https://127.0.0.1:{rogue.port}/")
            return 0

        await asyncio.to_thread(calls)
        await asyncio.sleep(0.05)
        for server in made:
            await server.close()
        return good.received, rogue.received

    good_bytes, rogue_bytes = asyncio.run(run())
    assert good_bytes > 0 and rogue_bytes == 0


@pytest.mark.asyncio
async def test_peer_check_and_client_certificate(
    ca: pki.CertificateAuthority, tmp_path: Path, servers: list[_Server]
) -> None:
    named, named_cert = _node(ca)
    other, other_cert = _node(ca)
    me, my_cert = _node(ca)
    cert_file, key_file = _files(tmp_path, "me", my_cert)
    good = await _serve(servers, _server_ctx(tmp_path, named_cert, ca, require=True))
    wrong = await _serve(servers, _server_ctx(tmp_path, other_cert, ca, require=True))
    ctx = client_context(ca.cert, cert_file=cert_file, key_file=key_file)
    ext = {"sni_hostname": peer_server_name(named)}
    async with httpx.AsyncClient(verify=ctx) as client:
        ok = await client.get(f"https://127.0.0.1:{good.port}/", extensions=ext)
        assert ok.status_code == 200
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://127.0.0.1:{wrong.port}/", extensions=ext)
    # a peer is never accepted as "the controller" either (a fresh client: a
    # pooled connection is reused per origin, whatever the request's name)
    async with httpx.AsyncClient(verify=ctx) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://127.0.0.1:{good.port}/x")
    await asyncio.sleep(0.05)
    assert wrong.received == 0
    presented = good.client_certs[0]
    assert presented is not None
    assert (
        pki.parse_node_id(
            __import__("cryptography.x509", fromlist=["x"]).load_der_x509_certificate(
                presented
            )
        )
        == me
    )


def test_peer_server_name_round_trip() -> None:
    node = str(new_node_id())
    name = peer_server_name(node)
    assert name == name.lower() and "_" not in name
    assert expected_identity(name) == node
    assert expected_identity(name.encode()) == node
    assert expected_identity(name.upper()) == node
    for other in (None, "", "127.0.0.1", pki.SERVER_NAME, "node-x.node.meshforge",
                  name + ".evil", "evil." + name):  # fmt: skip
        assert expected_identity(other) is None


@pytest.mark.asyncio
async def test_tls12_only_server_is_refused(
    ca: pki.CertificateAuthority, tmp_path: Path, servers: list[_Server]
) -> None:
    controller = pki.issue_server_cert(ca, hosts=["127.0.0.1"])
    ctx = _server_ctx(tmp_path, controller, ca, require=False)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    server = await _serve(servers, ctx)
    async with httpx.AsyncClient(verify=client_context(ca.cert)) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://127.0.0.1:{server.port}/")
