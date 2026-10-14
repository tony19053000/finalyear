"""V0.7 G7 criterion 1 sweep: the peer server (47120) refuses an **expired**
client certificate - the one 47120 cell of the criterion 1 matrix that had no
test (``docs/THREAT_MODEL.md`` §8; the other cells are in
``test_v07_g3_peer_tls.py`` and the revocation tests).

A real :class:`PeerBlobServer` with a fabric CA that dates from yesterday (so
it can issue an already expired certificate), a valid peer token minted for
the fetcher: an expired fetcher certificate gets nothing; the same fetcher
with a fresh certificate gets the blob (positive control).
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.security]

NOW = dt.datetime.now(dt.UTC)


def _issue(
    ca: pki.CertificateAuthority,
    tmp: Path,
    node: str,
    *,
    now: dt.datetime | None = None,
    lifetime: dt.timedelta = dt.timedelta(days=1),
) -> tuple[Path, Path]:
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node),
        node_id=node,
        fabric_id=ca.fabric_id,
        now=now,
        lifetime=lifetime,
    )
    folder = tmp / f"{node}-{cert.serial_number:x}"
    folder.mkdir()
    (folder / "node.crt").write_bytes(pki.cert_to_pem(cert))
    (folder / "node.key").write_bytes(pki.key_to_pem(key))
    (folder / "node.key").chmod(0o600)
    return folder / "node.crt", folder / "node.key"


async def test_an_expired_client_certificate_gets_nothing_from_the_peer_server(
    tmp_path: Path,
) -> None:
    ca = pki.create_ca(new_fabric_id(), now=NOW - dt.timedelta(days=1))
    holder, fetcher = str(new_node_id()), str(new_node_id())
    cas = AgentCas.at(tmp_path / "cas-data")
    await cas.prepare()
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(os.urandom(MIN_CHUNK_SIZE + 7))
    built = build_tree(root, chunk_size=MIN_CHUNK_SIZE)
    for hex_, path in built.sources.items():
        cas.store.put_file(path, Digest(hex_))
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    signing = Ed25519PrivateKey.generate()
    auth = PeerAuthorizer(cas, node_id=holder)
    assert auth.set_key(
        signing.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    server_cert, server_key = _issue(ca, tmp_path, holder)
    tls = peer_server_tls(
        ca.cert, cert_file=server_cert, key_file=server_key, revoked=frozenset
    )
    server = PeerBlobServer(
        cas,
        auth,
        PeerSettings.model_validate({"bind": "127.0.0.1", "port": 0}),
        node_id=holder,
        tls=tls,
    )
    await server.start()
    token = sign_peer_token(
        signing,
        tree=built.tree.tree_id,
        requester=fetcher,  # type: ignore[arg-type]
        holder=holder,  # type: ignore[arg-type]
        attempt_id=new_attempt_id(),
        now=time.time(),
    )
    blob = next(iter(built.tree.files())).digest
    url = f"https://127.0.0.1:{server.port}/v1/blobs/{blob.hex}"
    headers = {"authorization": f"Bearer {token}"}
    expect = {"sni_hostname": peer_server_name(holder)}
    try:
        cert, key = _issue(
            ca,
            tmp_path,
            fetcher,
            now=NOW - dt.timedelta(hours=2),
            lifetime=dt.timedelta(minutes=30),
        )
        expired = client_context(ca.cert, cert_file=cert, key_file=key)
        async with httpx.AsyncClient(verify=expired, timeout=5) as http:
            with pytest.raises(httpx.HTTPError):
                await http.get(url, headers=headers, extensions=expect)
        assert server.stats.served == 0

        cert, key = _issue(ca, tmp_path, fetcher)
        fresh = client_context(ca.cert, cert_file=cert, key_file=key)
        async with httpx.AsyncClient(verify=fresh, timeout=5) as http:
            ok = await http.get(url, headers=headers, extensions=expect)
        assert ok.status_code == 200
        assert server.stats.served == 1
    finally:
        await server.close()
