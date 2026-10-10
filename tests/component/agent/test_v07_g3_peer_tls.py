"""V0.7 G3 deliverable 4 (D139): the peer blob server (47120) speaks TLS 1.3 with
the node's certificate and requires a client certificate.

A real :class:`PeerBlobServer` over a real CAS, real ``httpx`` clients with the
agent's :func:`~meshforge.common.fabric_tls.client_context`:

* no client certificate / plaintext / a certificate from another CA -> no
  request is served;
* a fabric node's certificate + a valid peer token -> the blob;
* the fetching side insists the server is the node the controller named: a
  different node of the same fabric fails the handshake (nothing sent, no
  token leaked);
* a revoked client serial is refused at the connection (the set is read at
  every connection).
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mf_fabric import fabric_ca, node_material

from meshforge.agent.cas import AgentCas
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.peer.server import peer_server_tls
from meshforge.agent.settings import PeerSettings
from meshforge.common import pki
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.fabric_tls import client_context, peer_server_name
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_fabric_id, new_node_id
from meshforge.common.peer_token import sign_peer_token
from meshforge.common.tree import build_tree

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


def _files(tmp: Path, node: str) -> tuple[Path, Path]:
    key_pem, cert_pem = node_material(node)
    folder = tmp / node
    folder.mkdir(exist_ok=True)
    (folder / "node.key").write_bytes(key_pem)
    (folder / "node.key").chmod(0o600)
    (folder / "node.crt").write_bytes(cert_pem)
    return folder / "node.crt", folder / "node.key"


async def _server(
    tmp: Path, holder: str, revoked: set[str], requester: str
) -> tuple[PeerBlobServer, Digest, str]:
    """A TLS peer server of ``holder`` and a token minted for ``requester``
    (the fetcher whose certificate presents it, D155 T-2)."""
    cas = AgentCas.at(tmp / "cas-data")
    await cas.prepare()
    root = tmp / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(os.urandom(MIN_CHUNK_SIZE + 7))
    built = build_tree(root, chunk_size=MIN_CHUNK_SIZE)
    for hex_, path in built.sources.items():
        cas.store.put_file(path, Digest(hex_))
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    key = Ed25519PrivateKey.generate()
    auth = PeerAuthorizer(cas, node_id=holder)
    assert auth.set_key(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    cert, node_key = _files(tmp, holder)
    tls = peer_server_tls(
        fabric_ca().cert, cert_file=cert, key_file=node_key, revoked=lambda: revoked
    )
    cfg = PeerSettings.model_validate({"bind": "127.0.0.1", "port": 0})
    server = PeerBlobServer(cas, auth, cfg, node_id=holder, tls=tls)
    await server.start()
    import time

    token = sign_peer_token(
        key,
        tree=built.tree.tree_id,
        requester=requester,  # type: ignore[arg-type]
        holder=holder,  # type: ignore[arg-type]
        attempt_id=new_attempt_id(),
        now=time.time(),
    )
    return server, next(iter(built.tree.files())).digest, token


async def test_peer_server_tls_rules(tmp_path: Path) -> None:
    holder, fetcher = str(new_node_id()), str(new_node_id())
    revoked: set[str] = set()
    server, blob, token = await _server(tmp_path, holder, revoked, fetcher)
    url = f"https://127.0.0.1:{server.port}/v1/blobs/{blob.hex}"
    headers = {"authorization": f"Bearer {token}"}
    expect = {"sni_hostname": peer_server_name(holder)}
    cert, key = _files(tmp_path, fetcher)
    agent_ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        # the real thing: our node cert, the named peer, a valid token
        async with httpx.AsyncClient(verify=agent_ctx, timeout=5) as http:
            ok = await http.get(url, headers=headers, extensions=expect)
        assert ok.status_code == 200
        served = server.stats.served

        # no client certificate (TLS 1.3: the server refuses after the client
        # handshake; the request never gets an answer)
        no_cert = client_context(fabric_ca().cert)
        async with httpx.AsyncClient(verify=no_cert, timeout=5) as http:
            with pytest.raises(httpx.HTTPError):
                await http.get(url, headers=headers, extensions=expect)

        # plaintext
        async with httpx.AsyncClient(timeout=5) as http:
            with pytest.raises(httpx.HTTPError):
                await http.get(url.replace("https://", "http://"), headers=headers)

        # a client certificate from another CA
        other = pki.create_ca(new_fabric_id())
        stranger_id = str(new_node_id())
        stranger_key = pki.generate_key()
        stranger = pki.issue_node_cert(
            other,
            pki.build_csr(stranger_key, fabric_id=other.fabric_id, node_id=stranger_id),
            node_id=stranger_id,
            fabric_id=other.fabric_id,
        )
        (tmp_path / "s.crt").write_bytes(pki.cert_to_pem(stranger))
        (tmp_path / "s.key").write_bytes(pki.key_to_pem(stranger_key))
        alien = client_context(
            fabric_ca().cert, cert_file=tmp_path / "s.crt", key_file=tmp_path / "s.key"
        )
        async with httpx.AsyncClient(verify=alien, timeout=5) as http:
            with pytest.raises(httpx.HTTPError):
                await http.get(url, headers=headers, extensions=expect)

        # the server is not the node the controller named
        wrong = {"sni_hostname": peer_server_name(str(new_node_id()))}
        async with httpx.AsyncClient(verify=agent_ctx, timeout=5) as http:
            with pytest.raises(httpx.ConnectError):
                await http.get(url, headers=headers, extensions=wrong)

        # a revoked client serial: refused at the connection
        fetcher_serial = format(
            pki.cert_from_pem(node_material(fetcher)[1]).serial_number, "x"
        )
        revoked.add(fetcher_serial)
        async with httpx.AsyncClient(verify=agent_ctx, timeout=5) as http:
            with pytest.raises(httpx.HTTPError):
                await http.get(url, headers=headers, extensions=expect)
        assert server.stats.served == served  # nothing more was served
        assert server.stats.refused.get("peer_cert_revoked", 0) >= 1
    finally:
        await server.close()


async def test_a_serial_revoked_mid_connection_is_refused_at_its_next_request(
    tmp_path: Path,
) -> None:
    """V0.7 G3 deliverable 6 (D138): the revocation set is read again before
    every request, so a keep-alive connection opened before the revocation
    gets nothing more."""
    holder, fetcher = str(new_node_id()), str(new_node_id())
    revoked: set[str] = set()
    server, blob, token = await _server(tmp_path, holder, revoked, fetcher)
    url = f"https://127.0.0.1:{server.port}/v1/blobs/{blob.hex}"
    headers = {"authorization": f"Bearer {token}"}
    expect = {"sni_hostname": peer_server_name(holder)}
    cert, key = _files(tmp_path, fetcher)
    agent_ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        async with httpx.AsyncClient(verify=agent_ctx, timeout=5) as http:
            assert (
                await http.get(url, headers=headers, extensions=expect)
            ).status_code == 200
            connections = server.stats.connections
            revoked.add(
                format(pki.cert_from_pem(node_material(fetcher)[1]).serial_number, "x")
            )
            refused = await http.get(url, headers=headers, extensions=expect)
            assert refused.status_code == 403
        assert server.stats.connections == connections  # the same connection
        assert server.stats.refused.get("peer_cert_revoked", 0) == 1
    finally:
        await server.close()
