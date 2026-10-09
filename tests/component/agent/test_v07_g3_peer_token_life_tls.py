"""V0.7 G3 deliverable 5 (D147): a transfer longer than one peer-token life
keeps its peer when the tokens are re-minted - real TLS peer server.

A real :class:`PeerBlobServer` (TLS 1.3, client certificate required) over a
real CAS; a real :class:`BlobFetcher` with the peer as its ONLY source. The
holder's wall clock runs 20 s per request served, so the transfer spans many
token lives (60 s + 30 s skew) on the holder's own clock:

* with the re-minted tokens the attempt holds (``token_for``, what
  ``RefreshPeerTokens`` fills) every range carries a token minted "now" by
  the holder's clock - the whole file comes from the peer, nothing refused;
* without them (the pre-D147 behaviour) the first token expires mid-transfer:
  the peer answers ``expired`` and the peer-only transfer fails. This
  is the mutation check of the re-mint path.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mf_fabric import fabric_ca, node_material

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobFetcher, BlobSource, FetchError, FetchTuning
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.peer.server import peer_server_tls
from meshforge.agent.settings import PeerSettings
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.errors import FailureType
from meshforge.common.fabric_tls import client_context
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import MAX_TTL_S, SKEW_S, sign_peer_token
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE
CHUNKS = 24
START = 1_800_000_000.0
STEP_S = 20.0
"""Holder wall-clock seconds per request: 24 chunks span 480 s = 8 token lives."""


def _files(tmp: Path, node: str) -> tuple[Path, Path]:
    key_pem, cert_pem = node_material(node)
    folder = tmp / node
    folder.mkdir(exist_ok=True)
    (folder / "node.key").write_bytes(key_pem)
    (folder / "node.key").chmod(0o600)
    (folder / "node.crt").write_bytes(cert_pem)
    return folder / "node.crt", folder / "node.key"


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.holder = str(new_node_id())
        self.requester = str(new_node_id())
        self.attempt = str(new_attempt_id())
        self.key = Ed25519PrivateKey.generate()
        self.server: PeerBlobServer | None = None
        self.built: BuiltTree | None = None
        self.data = b""
        self.minted: list[int] = []

    def clock(self) -> float:
        """The holder's wall clock: STEP_S further per request it served."""
        served = self.server.stats.requests if self.server is not None else 0
        return START + STEP_S * served

    async def start(self) -> None:
        cas = AgentCas.at(self.tmp / "holder-cas")
        await cas.prepare()
        root = self.tmp / "tree"
        root.mkdir()
        self.data = os.urandom(CHUNKS * CS)
        (root / "big.bin").write_bytes(self.data)
        self.built = build_tree(root, chunk_size=CS)
        for hex_, path in self.built.sources.items():
            cas.store.put_file(path, Digest(hex_))
        cas.store.put_bytes(self.built.manifest, self.built.tree.tree_id)
        auth = PeerAuthorizer(cas, node_id=self.holder, wall_clock=self.clock)
        assert auth.set_key(
            self.key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        cert, key = _files(self.tmp, self.holder)
        tls = peer_server_tls(fabric_ca().cert, cert_file=cert, key_file=key)
        cfg = PeerSettings.model_validate(
            {"bind": "127.0.0.1", "port": 0, "idle_timeout_s": 5.0}
        )
        self.server = PeerBlobServer(cas, auth, cfg, node_id=self.holder, tls=tls)
        await self.server.start()

    def mint(self) -> str:
        """A token as the controller signs it: valid MAX_TTL_S from "now"."""
        assert self.built is not None
        now = self.clock()
        self.minted.append(int(now))
        return sign_peer_token(
            self.key,
            tree=self.built.tree.tree_id,
            requester=self.requester,  # type: ignore[arg-type]
            holder=self.holder,  # type: ignore[arg-type]
            attempt_id=self.attempt,  # type: ignore[arg-type]
            now=now,
            ttl_s=MAX_TTL_S,
        )

    async def fetch(
        self, token_for: Callable[[BlobSource], str | None] | None
    ) -> BlobFetcher:
        assert self.server is not None and self.built is not None
        cert, key = _files(self.tmp, self.requester)
        cas = AgentCas.at(self.tmp / f"fetcher-{len(self.minted)}")
        await cas.prepare()
        source = BlobSource(
            f"https://127.0.0.1:{self.server.port}", self.mint(), self.holder
        )
        async with httpx.AsyncClient(
            timeout=10,
            trust_env=False,
            verify=client_context(fabric_ca().cert, cert_file=cert, key_file=key),
        ) as client:
            fetcher = BlobFetcher(
                cas,
                [source],
                client,
                tuning=FetchTuning(per_source=1, endgame_after_s=600.0),
                token_for=token_for,
            )
            tree = await fetcher.tree(self.built.tree.tree_id)
            await fetcher.ensure_all(tree)
        big = next(e for e in self.built.tree.files() if e.path == "big.bin")
        assert cas.store.path_for(big.digest).read_bytes() == self.data
        return fetcher


async def test_a_transfer_longer_than_one_token_life_keeps_its_peer(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    await world.start()
    assert world.server is not None
    try:
        fetcher = await world.fetch(lambda _src: world.mint())
        stats = fetcher.stats.sources[world.holder]
        assert stats.dropped is None
        assert stats.units == CHUNKS  # every chunk from the peer
        assert world.server.stats.refused == {}
        # The transfer outlived many token lives on the holder's clock...
        span = world.clock() - START
        assert span > 4 * (MAX_TTL_S + SKEW_S), span
        # ...and no token was ever minted for longer than MAX_TTL_S.
        assert len(world.minted) > CHUNKS
    finally:
        await world.server.close()


async def test_without_reminted_tokens_the_first_token_expires_mid_transfer(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    await world.start()
    assert world.server is not None
    try:
        with pytest.raises(FetchError) as caught:
            await world.fetch(None)
        # A peer-only transfer whose peer refused: transfer_error naming the
        # refusal (a peer's 401/403 is never the controller's "unauthorized").
        assert caught.value.failure_type is FailureType.TRANSFER_ERROR
        assert "refused" in caught.value.message
        assert world.server.stats.refused.get("expired", 0) >= 1
        assert 1 <= world.server.stats.served < CHUNKS + 1
    finally:
        await world.server.close()
