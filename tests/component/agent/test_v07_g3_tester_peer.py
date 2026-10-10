"""V0.7 G3 tester: the agent peer server (47120) and its clients under hostile
certificates, TLS floors and token shapes. Real :class:`PeerBlobServer`, real
CAS, real TLS (no mocks).

Criterion 3 (mTLS on the peer plane) and D138/D139/D147. Defects are strict
xfails named T-n.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import time
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mf_fabric import fabric_ca, node_material
from mf_forged_certs import CLIENT, SERVER, Forged, forge, other_fabric_id

from meshforge.agent.cas import AgentCas
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.peer.server import peer_server_tls
from meshforge.agent.settings import PeerSettings
from meshforge.common import pki
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.fabric_tls import client_context, peer_server_name
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import (
    MAX_TTL_S,
    PeerTokenClaims,
    PeerTokenError,
    sign_peer_token,
    verify_peer_token,
)
from meshforge.common.tree import build_tree

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


def _write(tmp: Path, name: str, forged: Forged) -> tuple[Path, Path]:
    folder = tmp / name
    folder.mkdir(exist_ok=True)
    cert, key = folder / "node.crt", folder / "node.key"
    cert.write_bytes(forged.cert_pem)
    key.write_bytes(forged.key_pem)
    key.chmod(0o600)
    return cert, key


def _node_files(tmp: Path, node: str) -> tuple[Path, Path]:
    key_pem, cert_pem = node_material(node)
    folder = tmp / node
    folder.mkdir(exist_ok=True)
    (folder / "node.key").write_bytes(key_pem)
    (folder / "node.key").chmod(0o600)
    (folder / "node.crt").write_bytes(cert_pem)
    return folder / "node.crt", folder / "node.key"


class Rig:
    def __init__(self) -> None:
        self.server: PeerBlobServer
        self.blob: Digest
        self.key = Ed25519PrivateKey.generate()
        self.holder = str(new_node_id())
        self.revoked: set[str] = set()
        self.clock_offset = 0.0

    def token(self, requester: str | None = None, *, ttl_s: int = MAX_TTL_S) -> str:
        return sign_peer_token(
            self.key,
            tree=self.tree_id,
            requester=requester or str(new_node_id()),  # type: ignore[arg-type]
            holder=self.holder,  # type: ignore[arg-type]
            attempt_id=new_attempt_id(),
            now=time.time(),
            ttl_s=ttl_s,
        )

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.server.port}/v1/blobs/{self.blob.hex}"


async def _rig(tmp: Path, *, server_cert: Forged | None = None) -> Rig:
    rig = Rig()
    cas = AgentCas.at(tmp / "cas-data")
    await cas.prepare()
    root = tmp / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(os.urandom(MIN_CHUNK_SIZE + 7))
    built = build_tree(root, chunk_size=MIN_CHUNK_SIZE)
    for hex_, path in built.sources.items():
        cas.store.put_file(path, Digest(hex_))
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    rig.tree_id = built.tree.tree_id  # type: ignore[attr-defined]
    rig.blob = next(iter(built.tree.files())).digest
    auth = PeerAuthorizer(
        cas, node_id=rig.holder, wall_clock=lambda: time.time() + rig.clock_offset
    )
    assert auth.set_key(
        rig.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    if server_cert is None:
        cert, key = _node_files(tmp, rig.holder)
    else:
        cert, key = _write(tmp, "server", server_cert)
    tls = peer_server_tls(
        fabric_ca().cert, cert_file=cert, key_file=key, revoked=lambda: rig.revoked
    )
    cfg = PeerSettings.model_validate({"bind": "127.0.0.1", "port": 0})
    rig.server = PeerBlobServer(cas, auth, cfg, node_id=rig.holder, tls=tls)
    await rig.server.start()
    return rig


async def _get(
    rig: Rig, ctx: ssl.SSLContext, token: str, *, name: str | None = None
) -> httpx.Response:
    async with httpx.AsyncClient(verify=ctx, timeout=5) as http:
        return await http.get(
            rig.url,
            headers={"authorization": f"Bearer {token}"},
            extensions={"sni_hostname": name or peer_server_name(rig.holder)},
        )


# --------------------------------------------------------------- the TLS floor


async def test_peer_server_refuses_tls12_clients(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    fetcher = str(new_node_id())
    cert, key = _node_files(tmp_path, fetcher)
    try:

        def attempt() -> str:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
            try:
                with (
                    socket.create_connection(("127.0.0.1", rig.server.port), 5) as raw,
                    ctx.wrap_socket(raw) as tls,
                ):
                    return str(tls.version())
            except ssl.SSLError:
                return "refused"

        assert await asyncio.to_thread(attempt) == "refused"
    finally:
        await rig.server.close()


# ------------------------------------------------- hostile CLIENT certificates


@pytest.mark.parametrize(
    "shape",
    ["server_auth_only", "foreign_fabric_uri", "no_san", "controller_like"],
)
async def test_peer_server_serves_nothing_to_a_misshapen_client_cert(
    tmp_path: Path, shape: str
) -> None:
    rig = await _rig(tmp_path)
    forged = {
        "server_auth_only": lambda: forge(usages=[SERVER]),
        "foreign_fabric_uri": lambda: forge(fabric_id=other_fabric_id()),
        "no_san": lambda: forge(sans=[], usages=[CLIENT]),
        "controller_like": lambda: forge(
            sans=[x509.DNSName(pki.SERVER_NAME)], usages=[CLIENT, SERVER]
        ),
    }[shape]()
    cert, key = _write(tmp_path, shape, forged)
    ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        served = rig.server.stats.served
        try:
            response = await _get(rig, ctx, rig.token(forged.node_id))
        except httpx.HTTPError:
            response = None  # refused during the handshake
        if response is not None:
            assert response.status_code in (401, 403), response.status_code
        assert rig.server.stats.served == served
    finally:
        await rig.server.close()


# ------------------------------------------------- hostile SERVER certificates


async def test_fetcher_refuses_a_peer_with_the_right_node_id_in_another_fabric(
    tmp_path: Path,
) -> None:
    """The CA signed it, the node id is the one the controller named - but the
    URI names another fabric. Nothing (no token) may be sent."""
    holder_fabric = other_fabric_id()
    probe = Rig()
    impostor = forge(node_id=probe.holder, fabric_id=holder_fabric)
    rig = await _rig(tmp_path, server_cert=impostor)
    fetcher = str(new_node_id())
    cert, key = _node_files(tmp_path, fetcher)
    ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        # the rig has its own holder id; the impostor cert names probe.holder
        with pytest.raises(httpx.ConnectError):
            await _get(
                rig, ctx, rig.token(fetcher), name=peer_server_name(probe.holder)
            )
        assert rig.server.stats.served == 0
        # and the server cert not naming the dialled node at all
        with pytest.raises(httpx.ConnectError):
            await _get(rig, ctx, rig.token(fetcher))
    finally:
        await rig.server.close()


async def test_fetcher_refuses_a_peer_whose_cert_is_client_auth_only(
    tmp_path: Path,
) -> None:
    probe = Rig()
    impostor = forge(node_id=probe.holder, usages=[CLIENT])
    rig = await _rig(tmp_path, server_cert=impostor)
    fetcher = str(new_node_id())
    cert, key = _node_files(tmp_path, fetcher)
    ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        with pytest.raises(httpx.ConnectError):
            await _get(
                rig, ctx, rig.token(fetcher), name=peer_server_name(probe.holder)
            )
    finally:
        await rig.server.close()


async def test_controller_name_is_never_satisfied_by_a_peer_node_cert(
    tmp_path: Path,
) -> None:
    """An enrolled laptop must not pass the controller check (no DNS SAN)."""
    rig = await _rig(tmp_path)
    fetcher = str(new_node_id())
    cert, key = _node_files(tmp_path, fetcher)
    ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        with pytest.raises(httpx.ConnectError):
            await _get(rig, ctx, rig.token(fetcher), name="meshforge-controller")
        with pytest.raises(httpx.ConnectError):  # lookalike names fail closed too
            await _get(rig, ctx, rig.token(fetcher), name="node-xyz.node.meshforge")
    finally:
        await rig.server.close()


# ------------------------------------------------------------ token binding


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-2: the peer server never compares the token's requester (req) with the "
        "node id of the client certificate (agent/peer/server.py only logs it): a peer "
        "token minted for node B works for ANY enrolled node C that captured it "
        "(THREAT_MODEL PB-S1 'partly mitigated, G3 follow-up'; brief D139/§5c: "
        "'tokens scoped to the requester')."
    ),
)
async def test_peer_token_minted_for_b_is_refused_to_node_c(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    cert, key = _node_files(tmp_path, c)
    ctx_c = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        stolen = rig.token(requester=b)
        response = await _get(rig, ctx_c, stolen)
        assert response.status_code in (401, 403), response.status_code
    finally:
        await rig.server.close()


async def test_peer_token_for_the_certificates_own_node_is_served(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    b = str(new_node_id())
    cert, key = _node_files(tmp_path, b)
    ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        assert (await _get(rig, ctx, rig.token(requester=b))).status_code == 200
    finally:
        await rig.server.close()


# ------------------------------------------------------------- token life


async def test_signer_refuses_a_token_longer_than_sixty_seconds() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError):
        sign_peer_token(
            key,
            tree=Digest("0" * 64),
            requester=str(new_node_id()),  # type: ignore[arg-type]
            holder=str(new_node_id()),  # type: ignore[arg-type]
            attempt_id=new_attempt_id(),
            now=time.time(),
            ttl_s=MAX_TTL_S + 1,
        )


async def test_verifier_refuses_a_properly_signed_token_that_outlives_sixty_seconds() -> (
    None
):
    """A signer that erred (or a stolen signing key used badly) cannot produce a
    long-lived token: the verifier enforces the cap itself."""
    import base64

    key = Ed25519PrivateKey.generate()
    holder = str(new_node_id())
    now = time.time()
    claims = PeerTokenClaims(
        tree=Digest("a" * 64),
        requester=str(new_node_id()),  # type: ignore[arg-type]
        holder=holder,  # type: ignore[arg-type]
        attempt_id=new_attempt_id(),
        exp=int(now) + 3600,
    )
    payload = claims.canonical_bytes()

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    token = b64(payload) + "." + b64(key.sign(payload))
    with pytest.raises(PeerTokenError):
        verify_peer_token(key.public_key(), token, holder=holder, now=now)


async def test_expired_token_is_refused_over_tls_and_a_fresh_one_works(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    b = str(new_node_id())
    cert, key = _node_files(tmp_path, b)
    ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    try:
        token = rig.token(requester=b, ttl_s=5)
        assert (await _get(rig, ctx, token)).status_code == 200
        rig.clock_offset = 5 + 30 + 2  # beyond ttl + the 30 s skew
        assert (await _get(rig, ctx, token)).status_code == 403
        rig.clock_offset = 0
        assert (await _get(rig, ctx, rig.token(requester=b))).status_code == 200
    finally:
        await rig.server.close()


async def test_revocation_applies_to_a_serial_added_between_two_requests_on_one_connection(
    tmp_path: Path,
) -> None:
    """Sequential revoke -> next request on the SAME keep-alive socket, and
    several concurrent connections: all refused after the set changes."""
    rig = await _rig(tmp_path)
    b = str(new_node_id())
    cert, key = _node_files(tmp_path, b)
    ctx = client_context(fabric_ca().cert, cert_file=cert, key_file=key)
    serial = format(pki.cert_from_pem(node_material(b)[1]).serial_number, "x")
    try:
        token = rig.token(requester=b)
        async with httpx.AsyncClient(verify=ctx, timeout=5) as http:
            ext = {"sni_hostname": peer_server_name(rig.holder)}
            headers = {"authorization": f"Bearer {token}"}
            results = await asyncio.gather(
                *[http.get(rig.url, headers=headers, extensions=ext) for _ in range(6)]
            )
            assert {r.status_code for r in results} == {200}
            rig.revoked.add(serial)
            after = await asyncio.gather(
                *[http.get(rig.url, headers=headers, extensions=ext) for _ in range(6)],
                return_exceptions=True,
            )
        for r in after:
            assert isinstance(r, Exception) or r.status_code in (401, 403), r
    finally:
        await rig.server.close()
