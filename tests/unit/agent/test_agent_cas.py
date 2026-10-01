"""V0.5 G1 (D80/D84): agent blob fetch (chunked, verified, resumable) and the
copy into a workdir (hash while copying, corrupt cache evicted)."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from meshforge.agent.cas import (
    AgentCas,
    BlobFetcher,
    BlobSource,
    CorruptBlobError,
    MaterialiseError,
    materialise,
)
from meshforge.agent.cas.fetcher import FetchError
from meshforge.agent.execution.fetch import stage_tree, tree_sources
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.tree import BuiltTree, build_tree

CS = MIN_CHUNK_SIZE
WINDOWS = sys.platform.startswith("win")


def _cas(tmp_path: Path, name: str = "agent") -> AgentCas:
    cas = AgentCas(CasStore(tmp_path / name, max_blob_bytes=1 << 30))
    cas.store.prepare()
    return cas


def _project(tmp_path: Path) -> tuple[Path, BuiltTree]:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_bytes(b"print('hi')\n")
    (root / "big.bin").write_bytes(os.urandom(3 * CS + 17))
    (root / "sub").mkdir()
    (root / "sub" / "copy.py").write_bytes(b"print('hi')\n")  # same blob twice
    (root / "empty").mkdir()
    if not WINDOWS:
        (root / "run.sh").write_bytes(b"#!/bin/sh\n")
        (root / "run.sh").chmod(0o755)
        (root / "link.py").symlink_to("main.py")
    return root, build_tree(root, chunk_size=CS)


class _Server:
    """A blob source honouring Range like the controller (Starlette FileResponse)."""

    def __init__(self, built: BuiltTree) -> None:
        self.blobs = {h: p.read_bytes() for h, p in built.sources.items()}
        self.blobs[built.tree.tree_id.hex] = built.manifest
        self.served = 0
        self.requests: list[str] = []
        self.mutate: Callable[[str, int, bytes], bytes] | None = None
        self.fail_after: int | None = None
        self.token = "tok"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401)
        hex_ = request.url.path.rsplit("/", 1)[-1]
        data = self.blobs.get(hex_)
        if data is None:
            return httpx.Response(404)
        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            raise httpx.ReadError("link dropped")
        self.requests.append(hex_)
        rng = request.headers.get("range")
        status, start = 200, 0
        if rng:
            a, b = rng.removeprefix("bytes=").split("-")
            start = int(a)
            data = data[start : int(b) + 1]
            status = 206
        if self.mutate is not None:
            data = self.mutate(hex_, start, data)
        self.served += len(data)
        # A real (not pre-read) stream, like a socket: aiter_raw works on it.
        return httpx.Response(
            status,
            headers={"content-length": str(len(data))},
            stream=httpx.ByteStream(data),
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


SOURCE = BlobSource(base_url="http://ctl:1", token="tok")


@pytest.mark.asyncio
async def test_fetch_then_materialise_round_trip(tmp_path: Path) -> None:
    root, built = _project(tmp_path)
    server = _Server(built)
    cas = _cas(tmp_path)
    async with server.client() as client:
        fetcher = BlobFetcher(cas, [SOURCE], client)
        tree = await fetcher.tree(built.tree.tree_id)
        stats = await fetcher.ensure_all(tree)
    assert stats.blobs_fetched == len(built.sources) and stats.blobs_hit == 0
    dest = tmp_path / "work"
    result = materialise(cas.store, tree, dest)
    for entry in tree.files():
        got = dest.joinpath(*entry.path.split("/")).read_bytes()
        assert got == root.joinpath(*entry.path.split("/")).read_bytes()
    assert (dest / "empty").is_dir()
    assert result.files == len(list(tree.files()))
    if not WINDOWS:
        assert os.readlink(dest / "link.py") == "main.py"
        assert stat.S_IMODE((dest / "run.sh").stat().st_mode) == 0o755
        assert stat.S_IMODE((dest / "main.py").stat().st_mode) == 0o644


@pytest.mark.asyncio
async def test_second_fetch_of_the_same_tree_transfers_nothing(tmp_path: Path) -> None:
    _root, built = _project(tmp_path)
    server = _Server(built)
    cas = _cas(tmp_path)
    async with server.client() as client:
        first = BlobFetcher(cas, [SOURCE], client)
        await first.ensure_all(await first.tree(built.tree.tree_id))
        served = server.served
        again = BlobFetcher(cas, [SOURCE], client)
        tree = await again.tree(built.tree.tree_id)
        stats = await again.ensure_all(tree)
    assert server.served == served  # 0 bytes the second time (criterion 1)
    assert stats.manifest_hit and stats.blobs_fetched == 0
    assert stats.blobs_hit == len(built.sources)


@pytest.mark.asyncio
async def test_interrupted_chunked_fetch_resumes_without_resending(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    data = os.urandom(4 * CS)
    (root / "big.bin").write_bytes(data)
    built = build_tree(root, chunk_size=CS)
    server = _Server(built)
    cas = _cas(tmp_path)
    async with server.client() as client:
        fetcher = BlobFetcher(cas, [SOURCE], client)
        tree = await fetcher.tree(built.tree.tree_id)
        server.fail_after = 3  # manifest + 2 chunks, then the link drops
        with pytest.raises(FetchError) as info:
            await fetcher.ensure_all(tree)
        assert info.value.failure_type is FailureType.TRANSFER_ERROR
        server.fail_after = None
        before = server.served
        again = BlobFetcher(cas, [SOURCE], client)
        stats = await again.ensure_all(tree)
    assert stats.chunks_resumed == 2 and stats.chunks_fetched == 2
    assert server.served - before == 2 * CS  # completed chunks never re-sent
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data


@pytest.mark.asyncio
async def test_a_chunk_corrupted_in_flight_is_refetched_and_never_kept(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "big.bin").write_bytes(os.urandom(2 * CS))
    built = build_tree(root, chunk_size=CS)
    server = _Server(built)
    flips = {"left": 1}

    def corrupt_once(_hex: str, start: int, data: bytes) -> bytes:
        if start == CS and flips["left"]:
            flips["left"] -= 1
            return bytes([data[0] ^ 0xFF]) + data[1:]
        return data

    server.mutate = corrupt_once
    cas = _cas(tmp_path)
    async with server.client() as client:
        fetcher = BlobFetcher(cas, [SOURCE], client)
        stats = await fetcher.ensure_all(await fetcher.tree(built.tree.tree_id))
    assert stats.corrupt_chunks == 1 and stats.blobs_fetched == 1


@pytest.mark.asyncio
async def test_a_chunk_always_corrupt_fails_integrity_and_nothing_is_stored(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    data = os.urandom(2 * CS)
    (root / "big.bin").write_bytes(data)
    built = build_tree(root, chunk_size=CS)
    server = _Server(built)
    server.mutate = lambda _h, start, d: (
        bytes([d[0] ^ 1]) + d[1:] if start == CS and len(d) == CS else d
    )
    cas = _cas(tmp_path)
    async with server.client() as client:
        fetcher = BlobFetcher(cas, [SOURCE], client)
        tree = await fetcher.tree(built.tree.tree_id)
        with pytest.raises(FetchError) as info:
            await fetcher.ensure_all(tree)
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert not cas.store.has(sha256_bytes(data))


@pytest.mark.asyncio
async def test_a_whole_small_blob_corrupted_in_flight_is_integrity_error(
    tmp_path: Path,
) -> None:
    _root, built = _project(tmp_path)
    server = _Server(built)
    target = sha256_bytes(b"print('hi')\n").hex
    server.mutate = lambda h, _s, d: d + b"!" if h == target else d
    cas = _cas(tmp_path)
    async with server.client() as client:
        fetcher = BlobFetcher(cas, [SOURCE], client)
        tree = await fetcher.tree(built.tree.tree_id)
        with pytest.raises(FetchError) as info:
            await fetcher.ensure_all(tree)
    assert info.value.failure_type in (
        FailureType.INTEGRITY_ERROR,
        FailureType.TRANSFER_ERROR,  # more bytes than declared
    )
    assert not cas.store.has(Digest(target))


@pytest.mark.asyncio
async def test_a_tampered_manifest_and_a_wrong_token_are_typed(tmp_path: Path) -> None:
    _root, built = _project(tmp_path)
    server = _Server(built)
    tree_hex = built.tree.tree_id.hex
    server.mutate = lambda h, _s, d: (
        d.replace(b'"v":1', b'"v":2') if h == tree_hex else d
    )
    async with server.client() as client:
        fetcher = BlobFetcher(_cas(tmp_path), [SOURCE], client)
        with pytest.raises(FetchError) as info:
            await fetcher.tree(built.tree.tree_id)
        assert info.value.failure_type is FailureType.INTEGRITY_ERROR
        bad = BlobFetcher(
            _cas(tmp_path, "b"),
            [BlobSource(base_url="http://ctl:1", token="x")],
            client,
        )
        with pytest.raises(FetchError) as info:
            await bad.tree(built.tree.tree_id)
        assert info.value.failure_type is FailureType.UNAUTHORIZED


# --------------------------------------------------------------- materialise


async def _filled(tmp_path: Path) -> tuple[AgentCas, BuiltTree]:
    _root, built = _project(tmp_path)
    server = _Server(built)
    cas = _cas(tmp_path)
    async with server.client() as client:
        fetcher = BlobFetcher(cas, [SOURCE], client)
        await fetcher.ensure_all(await fetcher.tree(built.tree.tree_id))
    return cas, built


@pytest.mark.asyncio
async def test_materialise_detects_a_corrupt_cached_blob_and_evicts_it(
    tmp_path: Path,
) -> None:
    cas, built = await _filled(tmp_path)
    digest = sha256_bytes(b"print('hi')\n")
    path = cas.store.path_for(digest)
    path.chmod(0o600)
    path.write_bytes(b"print('HI')\n")  # same size, wrong bytes: real corruption
    dest = tmp_path / "work"
    with pytest.raises(CorruptBlobError) as info:
        materialise(cas.store, built.tree, dest)
    assert info.value.digest == digest
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert not cas.store.has(digest)
    assert not dest.exists()  # nothing of the corrupt tree is left


@pytest.mark.asyncio
async def test_materialise_disk_floor_max_accepted_and_max_plus_one(
    tmp_path: Path,
) -> None:
    cas, built = await _filled(tmp_path)
    total = built.tree.total_bytes
    materialise(
        cas.store,
        built.tree,
        tmp_path / "ok",
        free_disk_floor_bytes=10,
        disk_free=lambda _p: total + 10,
    )
    with pytest.raises(MaterialiseError) as info:
        materialise(
            cas.store,
            built.tree,
            tmp_path / "no",
            free_disk_floor_bytes=10,
            disk_free=lambda _p: total + 9,
        )
    assert info.value.failure_type is FailureType.DISK_FULL
    assert not (tmp_path / "no").exists()


@pytest.mark.asyncio
async def test_materialise_needs_an_empty_destination_and_copies(
    tmp_path: Path,
) -> None:
    cas, built = await _filled(tmp_path)
    dest = tmp_path / "work"
    dest.mkdir()
    (dest / "x").write_bytes(b"x")
    with pytest.raises(MaterialiseError):
        materialise(cas.store, built.tree, dest)
    assert (dest / "x").read_bytes() == b"x"  # never touched
    fresh = tmp_path / "fresh"
    materialise(cas.store, built.tree, fresh)
    # A copy, not a hard link: writing the workdir file leaves the cache intact.
    (fresh / "main.py").write_bytes(b"changed by the job")
    digest = sha256_bytes(b"print('hi')\n")
    assert cas.store.verify(digest)
    assert (
        os.stat(fresh / "main.py").st_ino != os.stat(cas.store.path_for(digest)).st_ino
    )


@pytest.mark.asyncio
async def test_stage_tree_refetches_a_corrupt_cached_blob_once(tmp_path: Path) -> None:
    _root, built = _project(tmp_path)
    server = _Server(built)
    cas = _cas(tmp_path)
    transport = httpx.MockTransport(server.handler)
    report = await stage_tree(
        cas,
        tree_id=str(built.tree.tree_id),
        sources=[SOURCE],
        dest=tmp_path / "w1",
        transport=transport,
        free_disk_floor_bytes=0,
    )
    assert report.blobs_fetched == len(built.sources)
    digest = sha256_bytes(b"print('hi')\n")
    cas.store.path_for(digest).chmod(0o600)
    cas.store.path_for(digest).write_bytes(b"print('HI')\n")
    report = await stage_tree(
        cas,
        tree_id=str(built.tree.tree_id),
        sources=[SOURCE],
        dest=tmp_path / "w2",
        transport=transport,
        free_disk_floor_bytes=0,
    )
    assert report.corrupt_refetches == 1 and report.blobs_fetched == 1
    assert (tmp_path / "w2" / "main.py").read_bytes() == b"print('hi')\n"


@pytest.mark.asyncio
async def test_stage_tree_rejects_a_malformed_tree_id(tmp_path: Path) -> None:
    with pytest.raises(FetchError) as info:
        await stage_tree(
            _cas(tmp_path), tree_id="sha256:nope", sources=[SOURCE], dest=tmp_path / "w"
        )
    assert info.value.failure_type is FailureType.INVALID_INPUT


def test_tree_sources_validation() -> None:
    spec = agent_pb2.ExecutionSpec(fetch_token="tok", blob_port=47110)
    (only,) = tree_sources(spec, "10.0.0.5")
    assert only.base_url == "http://10.0.0.5:47110" and only.token == "tok"
    spec.sources.add(url="http://h:1", token="t", node_id="")
    assert tree_sources(spec, None)[0].base_url == "http://h:1"
    for bad in (
        agent_pb2.BlobSource(url="file:///etc", token="t"),
        agent_pb2.BlobSource(url="http://h:1", token=""),
        agent_pb2.BlobSource(url="http://h:1", token="a b"),
        agent_pb2.BlobSource(url="http://h:1", token="t", node_id="x" * 41),
    ):
        broken = agent_pb2.ExecutionSpec(sources=[bad])
        with pytest.raises(FetchError) as info:
            tree_sources(broken, "h")
        assert info.value.failure_type is FailureType.INVALID_INPUT
    many = agent_pb2.ExecutionSpec(
        sources=[agent_pb2.BlobSource(url="http://h:1", token="t")] * 16
    )
    assert len(tree_sources(many, None)) == 16
    many.sources.add(url="http://h:1", token="t")
    with pytest.raises(FetchError):
        tree_sources(many, None)


@pytest.mark.asyncio
async def test_stage_tree_refetches_two_corrupt_cached_blobs(tmp_path: Path) -> None:
    """G1 fix D3: the budget is per blob (once each), not one per attempt."""
    _root, built = _project(tmp_path)
    server = _Server(built)
    cas = _cas(tmp_path)
    transport = httpx.MockTransport(server.handler)
    kwargs = {
        "tree_id": str(built.tree.tree_id),
        "sources": [SOURCE],
        "transport": transport,
        "free_disk_floor_bytes": 0,
    }
    await stage_tree(cas, dest=tmp_path / "w1", **kwargs)  # type: ignore[arg-type]
    big = next(e for e in built.tree.files() if e.path == "big.bin")
    for digest in (sha256_bytes(b"print('hi')\n"), big.digest):
        path = cas.store.path_for(digest)
        path.chmod(0o600)
        raw = bytearray(path.read_bytes())
        raw[0] ^= 0xFF
        path.write_bytes(bytes(raw))
    report = await stage_tree(cas, dest=tmp_path / "w2", **kwargs)  # type: ignore[arg-type]
    assert report.corrupt_refetches == 2 and report.blobs_fetched == 2
    assert (tmp_path / "w2" / "big.bin").read_bytes() == (
        _root / "big.bin"
    ).read_bytes()


@pytest.mark.asyncio
async def test_refetch_budget_once_per_blob_and_bounded(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client:
        fetcher = BlobFetcher(_cas(tmp_path), [SOURCE], client, max_refetches=2)
    a, b, c = (sha256_bytes(x) for x in (b"a", b"b", b"c"))
    assert fetcher.allow_refetch(a)
    assert not fetcher.allow_refetch(a)  # the same blob only once
    assert fetcher.allow_refetch(b)
    assert not fetcher.allow_refetch(c)  # total budget spent


@pytest.mark.asyncio
async def test_a_blob_corrupt_in_flight_twice_is_integrity_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "f.txt").write_bytes(b"payload")
    built = build_tree(root, chunk_size=CS)
    server = _Server(built)
    target = sha256_bytes(b"payload").hex
    server.mutate = lambda h, _s, d: b"PAYLOAD" if h == target else d
    cas = _cas(tmp_path)
    async with server.client() as client:
        fetcher = BlobFetcher(cas, [SOURCE], client)
        tree = await fetcher.tree(built.tree.tree_id)
        with pytest.raises(FetchError) as info:
            await fetcher.ensure_all(tree)
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert fetcher.stats.refetches == 1
    assert server.requests.count(target) == 2
    assert not cas.store.has(Digest(target))
