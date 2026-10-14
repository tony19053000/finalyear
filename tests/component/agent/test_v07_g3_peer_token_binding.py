"""V0.7 G3 fix round 1 (D155 T-2): a peer or probe token is bound to its
requester. Over TLS the peer server serves a token only to the node whose
client certificate it names as ``req``; any other enrolled node that captured
it is refused ``403`` (reason ``wrong_requester`` / ``probe_wrong_requester``),
before any CAS lookup, so the refusal says nothing about what this node holds.

Real :class:`PeerBlobServer`, real CAS, real TLS 1.3, real fixture-CA node
certificates. THREAT_MODEL PB-S1.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import time
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
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.fabric_tls import client_context, peer_server_name
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import sign_peer_token, sign_probe_token
from meshforge.common.tree import build_tree

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.security]


class Rig:
    """One TLS peer server (``holder``) and the material to talk to it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.holder = str(new_node_id())
        self.key = Ed25519PrivateKey.generate()
        self.server: PeerBlobServer
        self.tree: Digest
        self.blob: Digest

    def ctx(self, node: str) -> ssl.SSLContext:
        key_pem, cert_pem = node_material(node)
        folder = self.tmp / f"client-{node}"
        folder.mkdir(exist_ok=True)
        (folder / "node.key").write_bytes(key_pem)
        (folder / "node.key").chmod(0o600)
        (folder / "node.crt").write_bytes(cert_pem)
        return client_context(
            fabric_ca().cert,
            cert_file=folder / "node.crt",
            key_file=folder / "node.key",
        )

    def token(self, requester: str, tree: Digest | None = None) -> str:
        return sign_peer_token(
            self.key,
            tree=tree or self.tree,
            requester=requester,  # type: ignore[arg-type]
            holder=self.holder,  # type: ignore[arg-type]
            attempt_id=new_attempt_id(),
            now=time.time(),
        )

    def probe(self, requester: str) -> str:
        return sign_probe_token(
            self.key, requester=requester, holder=self.holder, now=time.time()
        )

    async def get(
        self, node: str, token: str, *, path: str | None = None
    ) -> httpx.Response:
        async with httpx.AsyncClient(verify=self.ctx(node), timeout=5) as http:
            return await http.get(
                f"https://127.0.0.1:{self.server.port}"
                + (path or f"/v1/blobs/{self.blob.hex}"),
                headers={"authorization": f"Bearer {token}"},
                extensions={"sni_hostname": peer_server_name(self.holder)},
            )


async def _rig(tmp: Path) -> Rig:
    rig = Rig(tmp)
    cas = AgentCas.at(tmp / "cas-data")
    await cas.prepare()
    root = tmp / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(os.urandom(MIN_CHUNK_SIZE + 7))
    built = build_tree(root, chunk_size=MIN_CHUNK_SIZE)
    for hex_, path in built.sources.items():
        cas.store.put_file(path, Digest(hex_))
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    rig.tree = built.tree.tree_id
    rig.blob = next(iter(built.tree.files())).digest
    auth = PeerAuthorizer(cas, node_id=rig.holder)
    assert auth.set_key(
        rig.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    key_pem, cert_pem = node_material(rig.holder)
    (tmp / "server.key").write_bytes(key_pem)
    (tmp / "server.key").chmod(0o600)
    (tmp / "server.crt").write_bytes(cert_pem)
    tls = peer_server_tls(
        fabric_ca().cert,
        cert_file=tmp / "server.crt",
        key_file=tmp / "server.key",
        revoked=frozenset,
    )
    cfg = PeerSettings.model_validate({"bind": "127.0.0.1", "port": 0})
    rig.server = PeerBlobServer(cas, auth, cfg, node_id=rig.holder, tls=tls)
    await rig.server.start()
    return rig


async def test_blob_token_is_served_only_to_its_requester(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    try:
        for_b = rig.token(b)
        stolen = await rig.get(c, for_b)
        assert stolen.status_code == 403 and stolen.content == b""
        assert rig.server.stats.refused.get("wrong_requester") == 1
        assert rig.server.stats.served == 0
        ok = await rig.get(b, for_b)  # the same token, its own node: served
        assert ok.status_code == 200 and len(ok.content) > 0
        assert rig.server.stats.served == 1
    finally:
        await rig.server.close()


async def test_wrong_requester_is_refused_before_any_cas_lookup(
    tmp_path: Path,
) -> None:
    """A captured token for a tree this node does not hold: the rightful
    requester learns ``404 no_tree``, the thief only ``403 wrong_requester``
    (no oracle on this node's CAS)."""
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    absent_tree = Digest("cd" * 32)
    try:
        token = rig.token(b, tree=absent_tree)
        member_path = f"/v1/blobs/{rig.blob.hex}"
        assert (await rig.get(b, token, path=member_path)).status_code == 404
        assert (await rig.get(c, token, path=member_path)).status_code == 403
        assert rig.server.stats.refused.get("wrong_requester") == 1
        assert rig.server.stats.refused.get("no_tree") == 1
    finally:
        await rig.server.close()


async def test_probe_token_is_served_only_to_its_requester(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    path = "/v1/probe?bytes=1000"
    try:
        token = rig.probe(b)
        assert (await rig.get(c, token, path=path)).status_code == 403
        assert rig.server.stats.refused.get("probe_wrong_requester") == 1
        assert rig.server.stats.probes_served == 0
        ok = await rig.get(b, token, path=path)
        assert ok.status_code == 200 and ok.content == bytes(1000)
        assert rig.server.stats.probes_served == 1
    finally:
        await rig.server.close()


async def test_binding_holds_per_request_on_one_keep_alive_connection(
    tmp_path: Path,
) -> None:
    """The client identity is the connection's; every request on it is checked
    against that identity, whatever token it carries."""
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    url = f"https://127.0.0.1:{rig.server.port}/v1/blobs/{rig.blob.hex}"
    ext = {"sni_hostname": peer_server_name(rig.holder)}
    try:
        async with httpx.AsyncClient(verify=rig.ctx(b), timeout=5) as http:

            async def get(token: str) -> int:
                headers = {"authorization": f"Bearer {token}"}
                return (
                    await http.get(url, headers=headers, extensions=ext)
                ).status_code

            assert await get(rig.token(b)) == 200
            connections = rig.server.stats.connections
            assert await get(rig.token(c)) == 403
            assert await get(rig.token(b)) == 200
            results = await asyncio.gather(*[get(rig.token(c)) for _ in range(4)])
            assert set(results) == {403}
        assert rig.server.stats.refused.get("wrong_requester") == 5
        assert rig.server.stats.connections >= connections
    finally:
        await rig.server.close()
