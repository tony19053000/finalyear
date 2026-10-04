"""V0.5 G4 tester round 1: the work-stealing fetcher against REAL sockets.

Sources are real HTTP servers: the production ``PeerBlobServer`` (a good peer,
and the stand-in for the controller) and a hostile raw-socket server that
misbehaves in one chosen way. The criteria (brief 0.5.3/0.5.4, criteria 3, 4, 6):

* every failure of a peer leaves the transfer finishing from the controller
  with the right bytes, and nothing unverified ever lands in the CAS;
* a transfer interrupted for good resumes: a verified chunk is never fetched
  again (bytes are counted at the servers, not taken from the fetcher's stats);
* D6: a transfer that outlives the peer token's expiry drops the peer, the
  controller finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobFetcher, BlobSource, FetchError, FetchTuning
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.settings import PeerSettings
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import sign_peer_token
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE
CHUNKS = 16
NOW = 1_800_000_000.0
NO_ENDGAME = FetchTuning(per_source=1, endgame_after_s=600.0, slow_min_units=1000)


def _raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _project(tmp_path: Path) -> tuple[BuiltTree, bytes]:
    root = tmp_path / "proj"
    root.mkdir()
    big = os.urandom(CHUNKS * CS)
    (root / "big.bin").write_bytes(big)
    return build_tree(root, chunk_size=CS), big


@dataclass
class Holder:
    """A real peer server in front of its own CAS (also used as the controller)."""

    cas: AgentCas
    server: PeerBlobServer
    node_id: str
    key: Ed25519PrivateKey
    clock: Callable[[], float]
    tree: Digest
    holds: list[object] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    def source(self, *, peer: bool = True, ttl_s: int = 600) -> BlobSource:
        token = sign_peer_token(
            self.key,
            tree=self.tree,
            requester=new_node_id(),
            holder=self.node_id,
            attempt_id=new_attempt_id(),
            now=NOW,
            ttl_s=ttl_s,
        )
        return BlobSource(self.url, token, self.node_id if peer else "")

    def blob_path(self, digest: Digest) -> Path:
        return self.cas.store.path_for(digest)


async def _holder(
    tmp_path: Path,
    name: str,
    built: BuiltTree,
    *,
    clock: Callable[[], float] | None = None,
) -> Holder:
    cas = AgentCas(CasStore(tmp_path / name, max_blob_bytes=1 << 30))
    cas.store.prepare()
    for hex_, path in built.sources.items():
        cas.store.put_file(path, Digest(hex_))
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    key = Ed25519PrivateKey.generate()
    node_id = new_node_id()
    wall = clock or (lambda: NOW)
    auth = PeerAuthorizer(cas, node_id=node_id, wall_clock=wall)
    auth.set_key(_raw(key))
    cfg = PeerSettings.model_validate(
        {"bind": "127.0.0.1", "port": 0, "idle_timeout_s": 5.0}
    )
    server = PeerBlobServer(cas, auth, cfg, node_id=node_id)
    await server.start()
    return Holder(cas, server, node_id, key, wall, built.tree.tree_id)


def _fresh_cas(tmp_path: Path, name: str = "fetcher-cas") -> AgentCas:
    cas = AgentCas(CasStore(tmp_path / name, max_blob_bytes=1 << 30))
    cas.store.prepare()
    return cas


def _assert_cas_clean(cas: AgentCas, built: BuiltTree) -> None:
    """Every blob present in the CAS hashes to its name (nothing unverified)."""
    root = cas.store.root
    for path in root.rglob("*"):
        if path.is_file() and len(path.name) == 64 and path.parent.name != "tmp":
            assert hashlib.sha256(path.read_bytes()).hexdigest() == path.name, path


async def _run(
    cas: AgentCas,
    sources: list[BlobSource],
    built: BuiltTree,
    *,
    tuning: FetchTuning = NO_ENDGAME,
    timeout_s: float = 8.0,
) -> BlobFetcher:
    async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
        fetcher = BlobFetcher(cas, sources, client, tuning=tuning)
        tree = await fetcher.tree(built.tree.tree_id)
        await fetcher.ensure_all(tree)
    return fetcher


def _big_digest(built: BuiltTree) -> Digest:
    return next(e for e in built.tree.files() if e.path == "big.bin").digest


# ------------------------------------------------------------ hostile server


Behaviour = Callable[
    [asyncio.StreamWriter, str, "tuple[int, int] | None", bytes], Awaitable[None]
]


class Hostile:
    """A raw HTTP/1.1 server that answers range requests for ``blobs`` wrongly."""

    def __init__(self, blobs: dict[str, bytes], behave: Behaviour) -> None:
        self.blobs = blobs
        self.behave = behave
        self.requests: list[dict[str, str]] = []
        self.sent = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def source(self, node: str = "") -> BlobSource:
        return BlobSource(f"http://127.0.0.1:{self.port}", "hostile-token", node)

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                lines = head.decode("latin-1").split("\r\n")
                path = lines[0].split()[1]
                hdrs = {
                    k.lower(): v.strip()
                    for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)
                }
                hdrs["path"] = path
                self.requests.append(hdrs)
                span = None
                rng = hdrs.get("range")
                if rng:
                    a, _, b = rng.removeprefix("bytes=").partition("-")
                    span = (int(a), int(b))
                await self.behave(writer, path.rsplit("/", 1)[-1], span, b"")
                with contextlib.suppress(ConnectionError):
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            self._tasks.discard(task)

    def respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        extra: str = "",
        *,
        length: int | None = None,
    ) -> None:
        reason = {200: "OK", 206: "Partial Content", 302: "Found"}.get(status, "X")
        n = len(body) if length is None else length
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\ncontent-length: {n}\r\n{extra}\r\n".encode()
            + body
        )
        self.sent += len(body)


def _slice(blob: bytes, span: tuple[int, int] | None) -> bytes:
    return blob if span is None else blob[span[0] : span[1] + 1]


@pytest_asyncio.fixture
async def world(tmp_path: Path) -> AsyncIterator[tuple[BuiltTree, bytes, Holder]]:
    built, big = _project(tmp_path)
    ctl = await _holder(tmp_path, "ctl-cas", built)
    try:
        yield built, big, ctl
    finally:
        await ctl.server.close()


def _blobs(built: BuiltTree, big: bytes) -> dict[str, bytes]:
    out = {_big_digest(built).hex: big, built.tree.tree_id.hex: built.manifest}
    return out


async def _hostile(
    built: BuiltTree, big: bytes, kind: str, *, good_chunks: int = 0
) -> Hostile:
    state = {"n": 0}
    holder: dict[str, Hostile] = {}

    async def behave(
        writer: asyncio.StreamWriter, hex_: str, span: tuple[int, int] | None, _b: bytes
    ) -> None:
        h = holder["h"]
        data = h.blobs.get(hex_)
        if data is None:
            h.respond(writer, 404, b"")
            return
        correct = _slice(data, span)
        is_chunk = span is not None and hex_ == _big_digest(built).hex
        if is_chunk:
            state["n"] += 1
            if state["n"] <= good_chunks:
                h.respond(writer, 206, correct)
                return
        if kind == "whole_for_range":
            h.respond(writer, 200, data)
        elif kind == "oversized_206":
            h.respond(writer, 206, correct + b"EXTRA" * 1000)
        elif kind == "short_206":
            h.respond(writer, 206, correct[:-7], length=len(correct))
            writer.close()
        elif kind == "mid_chunk_reset":
            h.respond(writer, 206, correct[: len(correct) // 2], length=len(correct))
            await writer.drain()
            writer.transport.abort()
        elif kind == "huge_length":
            writer.write(
                f"HTTP/1.1 206 Partial Content\r\ncontent-length: {1 << 40}\r\n\r\n".encode()
            )
            for _ in range(256):  # ~256 MiB of zeros at most, then give up
                writer.write(b"\0" * (1 << 20))
                h.sent += 1 << 20
                await writer.drain()
        elif kind == "flipped_bit":
            bad = bytearray(correct)
            bad[len(bad) // 2] ^= 1
            h.respond(writer, 206, bytes(bad))
        elif kind == "gzip":
            import gzip

            h.respond(writer, 206, gzip.compress(correct), "content-encoding: gzip\r\n")
        elif kind == "redirect":
            h.respond(
                writer, 302, b"", f"location: http://127.0.0.1:{h.port}/spy/{hex_}\r\n"
            )
        elif kind == "hang":
            await asyncio.sleep(3600)
        elif kind == "403":
            h.respond(writer, 403, b"")
        elif kind == "500":
            h.respond(writer, 500, b"boom")
        else:  # pragma: no cover
            raise AssertionError(kind)

    h = Hostile(_blobs(built, big), behave)
    holder["h"] = h
    await h.start()
    return h


# -------------------------------------------------------- hostile peer cases

KINDS = [
    "whole_for_range",
    "oversized_206",
    "short_206",
    "mid_chunk_reset",
    "huge_length",
    "flipped_bit",
    "gzip",
    "redirect",
    "403",
    "500",
]


@pytest.mark.parametrize("kind", KINDS)
async def test_hostile_peer_never_corrupts_and_the_controller_finishes(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder], kind: str
) -> None:
    built, big, ctl = world
    peer = await _hostile(built, big, kind)
    cas = _fresh_cas(tmp_path)
    try:
        started = time.monotonic()
        fetcher = await _run(
            cas,
            [peer.source("node_hostile"), ctl.source(peer=False)],
            built,
            tuning=FetchTuning(
                per_source=2, endgame_after_s=600.0, slow_min_units=1000
            ),
        )
        assert time.monotonic() - started < 30
        digest = _big_digest(built)
        assert cas.store.path_for(digest).read_bytes() == big
        _assert_cas_clean(cas, built)
        stats = fetcher.stats.sources
        assert stats["node_hostile"].units == 0, (kind, stats)
        assert stats["node_hostile"].dropped is not None or kind == "whole_for_range"
        assert stats["controller"].units >= CHUNKS - 1 or stats["controller"].units > 0
        if kind == "huge_length":
            # A hostile Content-Length of 1 TiB must be cut near the chunk size.
            assert peer.sent < 40 * 1024 * 1024
        if kind == "redirect":
            assert not any(r["path"].startswith("/spy/") for r in peer.requests)
    finally:
        await peer.close()


async def test_hanging_peer_does_not_hold_the_transfer_hostage(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    built, big, ctl = world
    peer = await _hostile(built, big, "hang")
    cas = _fresh_cas(tmp_path)
    try:
        started = time.monotonic()
        fetcher = await _run(
            cas,
            [ctl.source(peer=False), peer.source("node_hang")],
            built,
            tuning=FetchTuning(per_source=2, endgame_after_s=0.3, slow_min_units=2),
            timeout_s=30.0,  # the production read timeout is long: end-game must win
        )
        elapsed = time.monotonic() - started
        assert elapsed < 12, f"{elapsed:.1f}s: the hung peer stalled the transfer"
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        assert fetcher.stats.sources["node_hang"].units == 0
    finally:
        await peer.close()


async def test_all_peers_fail_and_controller_completes(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    built, big, ctl = world
    peers = [
        await _hostile(built, big, k) for k in ("flipped_bit", "403", "mid_chunk_reset")
    ]
    # And a closed port: nothing listens there.
    dead = BlobSource("http://127.0.0.1:1", "x", "node_dead")
    cas = _fresh_cas(tmp_path)
    try:
        sources = [p.source(f"node_{i}") for i, p in enumerate(peers)]
        fetcher = await _run(cas, [*sources, dead, ctl.source(peer=False)], built)
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        _assert_cas_clean(cas, built)
        assert fetcher.stats.sources["controller"].units == CHUNKS
        assert all(
            s.dropped for n, s in fetcher.stats.sources.items() if n != "controller"
        )
    finally:
        for p in peers:
            await p.close()


async def test_every_source_failing_is_a_typed_error_and_leaves_no_blob(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    built, big, ctl = world
    peer = await _hostile(built, big, "flipped_bit")
    await ctl.server.close()  # the controller is gone too
    cas = _fresh_cas(tmp_path)
    try:
        with pytest.raises(FetchError) as exc:
            await _run(
                cas,
                [peer.source("node_bad"), ctl.source(peer=False)],
                built,
                timeout_s=3.0,
            )
        assert exc.value.failure_type in (
            FailureType.TRANSFER_ERROR,
            FailureType.INTEGRITY_ERROR,
            FailureType.TIMEOUT,
        )
        assert cas.store.size(_big_digest(built)) is None
        _assert_cas_clean(cas, built)
    finally:
        await peer.close()


# ------------------------------------------------------------------- resume


async def test_resume_never_refetches_a_verified_chunk_counted_at_the_servers(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    """Fetch 1 has only a peer that serves 6 chunks then dies mid-chunk (and no
    controller): it fails. Fetch 2 (controller only) must ask for the other
    10 chunks only. Bytes are what the controller's server really sent."""
    built, big, ctl = world
    good = 6
    peer = await _hostile(built, big, "mid_chunk_reset", good_chunks=good)
    cas = _fresh_cas(tmp_path)
    try:
        # The manifest comes from the controller in both runs; then the peer-only
        # transfer fails after `good` verified chunks.
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            first = BlobFetcher(
                cas,
                [peer.source("node_dies"), ctl.source(peer=False)],
                client,
                tuning=NO_ENDGAME,
            )
            tree = await first.tree(built.tree.tree_id)
        # Run only against the dying peer: drop the controller by not listing it.
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            peer_only = BlobFetcher(
                cas, [peer.source("node_dies")], client, tuning=NO_ENDGAME
            )
            with pytest.raises(FetchError):
                await peer_only.ensure_all(tree)
        assert cas.store.size(_big_digest(built)) is None
        before = ctl.server.stats.bytes_sent
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            second = BlobFetcher(
                cas, [ctl.source(peer=False)], client, tuning=NO_ENDGAME
            )
            await second.ensure_all(tree)
        from_ctl = ctl.server.stats.bytes_sent - before
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        assert second.stats.chunks_resumed >= good
        assert from_ctl <= (CHUNKS - good) * CS, (
            f"controller sent {from_ctl} B; only {(CHUNKS - good) * CS} B were missing"
        )
        assert from_ctl >= (CHUNKS - good - 1) * CS  # and it really was the rest
    finally:
        await peer.close()


async def test_rot_in_a_resumed_part_file_is_never_materialised_and_a_retry_succeeds(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    built, big, ctl = world
    peer = await _hostile(built, big, "mid_chunk_reset", good_chunks=CHUNKS - 2)
    cas = _fresh_cas(tmp_path)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            tree = await BlobFetcher(
                cas, [ctl.source(peer=False)], client, tuning=NO_ENDGAME
            ).tree(built.tree.tree_id)
            with pytest.raises(FetchError):
                await BlobFetcher(
                    cas, [peer.source("node_dies")], client, tuning=NO_ENDGAME
                ).ensure_all(tree)
        parts = [
            p
            for p in cas.store.root.rglob("*")
            if p.is_file() and p.suffix in (".part", "") and "tmp" in p.parts
        ]
        assert parts, "the interrupted transfer must leave its verified chunks"
        # Flip a byte inside an already-verified chunk of the .part file (disk rot).
        part = max(parts, key=lambda p: p.stat().st_size)
        with part.open("r+b") as fh:
            fh.seek(3 * CS + 100)
            byte = fh.read(1)
            fh.seek(3 * CS + 100)
            fh.write(bytes([byte[0] ^ 0xFF]))
        # Rot inside a chunk the part file calls verified: the whole-blob check at
        # assembly must catch it, discard the part, and a retry must succeed.
        outcomes: list[str] = []
        for _attempt in range(3):
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                try:
                    await BlobFetcher(
                        cas, [ctl.source(peer=False)], client, tuning=NO_ENDGAME
                    ).ensure_all(tree)
                except FetchError:
                    outcomes.append("error")
                    continue
            outcomes.append("ok")
            break
        assert outcomes[-1] == "ok", outcomes
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        _assert_cas_clean(cas, built)
    finally:
        await peer.close()


# ------------------------------------------------------- real good peers


async def test_two_real_peers_and_controller_share_the_work_byte_exact(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    built, big, ctl = world
    a = await _holder(tmp_path, "a-cas", built)
    b = await _holder(tmp_path, "b-cas", built)
    cas = _fresh_cas(tmp_path)
    try:
        fetcher = await _run(
            cas,
            [a.source(), b.source(), ctl.source(peer=False)],
            built,
            tuning=FetchTuning(
                per_source=2, endgame_after_s=600.0, slow_min_units=1000
            ),
        )
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        sent = sum(h.server.stats.bytes_sent for h in (a, b, ctl))
        # Payload + the manifest once; nothing fetched twice (end-game off).
        assert sent == len(big) + len(built.manifest)
        assert fetcher.stats.bytes_fetched == sent
        got = fetcher.stats.sources
        assert (
            got[a.node_id].bytes + got[b.node_id].bytes + got["controller"].bytes
            == sent
        )
        assert sum(s.units for s in got.values()) == CHUNKS
    finally:
        await a.server.close()
        await b.server.close()


async def test_peer_with_a_blob_deleted_behind_the_index_is_excluded_for_that_blob_only(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    """Index lie: the peer holds the manifest but its blob file vanished (not
    through the CAS, so no cache delta). 404 per blob; the controller covers it."""
    built, big, ctl = world
    a = await _holder(tmp_path, "a-cas", built)
    a.blob_path(_big_digest(built)).unlink()
    cas = _fresh_cas(tmp_path)
    try:
        fetcher = await _run(cas, [a.source(), ctl.source(peer=False)], built)
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        assert fetcher.stats.sources[a.node_id].units == 0
        assert fetcher.stats.sources["controller"].units == CHUNKS
    finally:
        await a.server.close()


async def test_peer_with_rotted_blob_on_disk_is_detected_per_chunk(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    """Criterion 4 with the real server: bit rot in the holder's CAS file."""
    built, big, ctl = world
    a = await _holder(tmp_path, "a-cas", built)
    path = a.blob_path(_big_digest(built))
    path.chmod(0o600)
    rotted = bytearray(big)
    for chunk in (2, 9):
        rotted[chunk * CS + 5] ^= 0x40
    path.write_bytes(bytes(rotted))
    cas = _fresh_cas(tmp_path)
    try:
        fetcher = await _run(
            cas,
            [a.source(), ctl.source(peer=False)],
            built,
            tuning=FetchTuning(
                per_source=2, endgame_after_s=600.0, slow_min_units=1000
            ),
        )
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        _assert_cas_clean(cas, built)
        assert fetcher.stats.corrupt_chunks >= 1
        assert fetcher.stats.sources[a.node_id].dropped is not None
    finally:
        await a.server.close()


# ---------------------------------------------------------------------- D6


async def test_d6_transfer_outliving_the_peer_token_drops_the_peer_and_finishes(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    built, big, ctl = world
    served_at: list[int] = []
    holder_ref: dict[str, Holder] = {}

    def clock() -> float:
        # The peer's wall clock jumps past the token's expiry after 5 requests.
        h = holder_ref.get("h")
        return NOW + (3600 if h is not None and h.server.stats.requests > 5 else 0)

    a = await _holder(tmp_path, "a-cas", built, clock=clock)
    holder_ref["h"] = a
    cas = _fresh_cas(tmp_path)
    try:
        fetcher = await _run(
            cas,
            [a.source(ttl_s=60), ctl.source(peer=False)],
            built,
            tuning=NO_ENDGAME,
        )
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        stats = fetcher.stats.sources[a.node_id]
        assert stats.dropped == FailureType.UNAUTHORIZED.value
        assert 1 <= stats.units < CHUNKS
        assert a.server.stats.refused.get("expired", 0) >= 1
        served_at.append(stats.units)
        # No verified chunk was fetched twice: peer + controller bytes = payload.
        sent = a.server.stats.bytes_sent + ctl.server.stats.bytes_sent
        assert sent <= len(big) + len(built.manifest) + CS, sent
    finally:
        await a.server.close()


async def test_peer_server_stopped_mid_transfer_controller_finishes_counted(
    tmp_path: Path, world: tuple[BuiltTree, bytes, Holder]
) -> None:
    built, big, ctl = world
    a = await _holder(tmp_path, "a-cas", built)
    cas = _fresh_cas(tmp_path)

    async def killer() -> None:
        while a.server.stats.served < 4:  # noqa: ASYNC110
            await asyncio.sleep(0.002)
        await a.server.close()

    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            fetcher = BlobFetcher(
                cas,
                [a.source(), ctl.source(peer=False)],
                client,
                tuning=FetchTuning(
                    per_source=2, endgame_after_s=600.0, slow_min_units=1000
                ),
            )
            tree = await fetcher.tree(built.tree.tree_id)
            task = asyncio.create_task(killer())
            await fetcher.ensure_all(tree)
            await task
        assert cas.store.path_for(_big_digest(built)).read_bytes() == big
        ctl_chunks_bytes = ctl.server.stats.bytes_sent
        assert fetcher.stats.sources[a.node_id].units >= 1
        # The controller sent each missing chunk once (+ manifest if it served it).
        assert ctl_chunks_bytes <= len(big) + len(built.manifest)
        assert fetcher.stats.sources["controller"].bytes + fetcher.stats.sources[
            a.node_id
        ].bytes >= len(big)
    finally:
        await a.server.close()
