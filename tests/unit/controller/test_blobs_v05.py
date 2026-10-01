"""V0.5 G1 (D80-D83): controller blob store parts, trees and tree-scoped tokens."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.tree import TreeError, build_tree
from meshforge.controller.blobs import (
    BlobError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobStore,
    BlobTooLargeError,
    FetchTokens,
)

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _store(tmp_path: Path, cap: int = 64 * 1024 * 1024) -> BlobStore:
    store = BlobStore(tmp_path / "blobs", max_blob_bytes=cap)
    store.prepare()
    return store


def _pieces(data: bytes) -> list[tuple[bytes, str]]:
    return [
        (data[i : i + CS], hashlib.sha256(data[i : i + CS]).hexdigest())
        for i in range(0, len(data), CS)
    ]


async def _put(store: BlobStore, data: bytes) -> Digest:
    digest = sha256_bytes(data)
    await store.put(digest, _one(data), declared_size=len(data))
    return digest


async def test_missing_is_ordered_deduplicated_and_exact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    have = await _put(store, b"have")
    a, b = sha256_bytes(b"a"), sha256_bytes(b"b")
    assert store.missing([a, have, b, a]) == [a, b]


async def test_parts_upload_resume_and_complete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(3 * CS + 11)
    digest = sha256_bytes(data)
    pieces = _pieces(data)
    assert await store.part_status(digest) is None
    for i in (0, 2):
        chunk, hex_ = pieces[i]
        assert await store.put_part(
            digest, i, size=len(data), chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
        )
    # A duplicate part is accepted and not written twice.
    chunk, hex_ = pieces[0]
    assert not await store.put_part(
        digest, 0, size=len(data), chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
    )
    status = await store.part_status(digest)
    assert status is not None and status.received == (0, 2) and not status.complete
    with pytest.raises(BlobError) as info:
        await store.complete_parts(digest)
    assert info.value.failure_type is FailureType.CONFLICT
    # "Restart": a new store object resumes from the sidecar.
    again = _store(tmp_path)
    for i in (1, 3):
        chunk, hex_ = pieces[i]
        await again.put_part(
            digest, i, size=len(data), chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
        )
    result = await again.complete_parts(digest)
    assert result.created and again.path_for(digest).read_bytes() == data
    status = await again.part_status(digest)
    assert status is not None and status.complete
    assert again.parts_received == 2


async def test_a_corrupt_part_is_refused_and_not_marked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(2 * CS)
    digest = sha256_bytes(data)
    chunk, hex_ = _pieces(data)[0]
    bad = bytes([chunk[0] ^ 1]) + chunk[1:]
    with pytest.raises(BlobIntegrityError):
        await store.put_part(
            digest, 0, size=len(data), chunk_size=CS, chunk_hex=hex_, body=_one(bad)
        )
    status = await store.part_status(digest)
    assert status is None or status.received == ()


async def test_parts_whose_whole_does_not_match_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(2 * CS)
    claimed = sha256_bytes(b"not these bytes")
    for i, (chunk, hex_) in enumerate(_pieces(data)):
        await store.put_part(
            claimed, i, size=len(data), chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
        )
    with pytest.raises(BlobIntegrityError):
        await store.complete_parts(claimed)
    assert store.size(claimed) is None
    assert await store.part_status(claimed) is None  # starts over


async def test_part_bounds(tmp_path: Path) -> None:
    store = _store(tmp_path, cap=2 * CS)
    data = os.urandom(2 * CS)
    digest = sha256_bytes(data)
    chunk, hex_ = _pieces(data)[0]
    with pytest.raises(BlobTooLargeError):  # blob over the cap: max+1
        await store.put_part(
            digest, 0, size=2 * CS + 1, chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
        )
    with pytest.raises(BlobError):  # index out of range
        await store.put_part(
            digest, 2, size=2 * CS, chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
        )
    with pytest.raises(BlobError):  # body longer than a chunk
        await store.put_part(
            digest,
            0,
            size=2 * CS,
            chunk_size=CS,
            chunk_hex=hex_,
            body=_one(chunk + b"x"),
        )
    with pytest.raises(BlobNotFoundError):
        await store.complete_parts(sha256_bytes(b"never started"))


async def test_tree_load_validate_and_members(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.txt").write_bytes(b"A")
    (project / "b.txt").write_bytes(b"B")
    built = build_tree(project)
    tree_id = built.tree.tree_id
    assert await store.tree(tree_id) is None
    await _put(store, built.manifest)
    tree = await store.tree(tree_id)
    assert tree is not None and tree.tree_id == tree_id
    members = await store.tree_members(tree_id)
    assert members == {sha256_bytes(b"A").hex, sha256_bytes(b"B").hex}


async def test_a_bundle_or_garbage_root_grants_no_members(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bundle = b"\x28\xb5\x2f\xfd" + os.urandom(100)  # zstd magic: a bundle
    garbage = b'{"not": "a tree"}'
    for data in (bundle, garbage):
        digest = await _put(store, data)
        assert await store.tree_members(digest) == frozenset()
    with pytest.raises(TreeError):
        await store.tree(sha256_bytes(garbage))


async def test_fetch_token_grant_expires() -> None:
    now = [100.0]
    tokens = FetchTokens(clock=lambda: now[0])
    root = sha256_bytes(b"tree")
    token = tokens.mint("attempt_x", [root], ttl_s=10).get_secret_value()
    grant = tokens.grant(token)
    assert grant is not None and root.hex in grant.digests
    assert tokens.grant("nope") is None and tokens.grant(None) is None
    now[0] = 111.0
    assert tokens.grant(token) is None


async def test_concurrent_duplicate_completes_all_succeed(tmp_path: Path) -> None:
    """G1 fix D2: once the blob is stored, every complete answers success."""
    import asyncio

    store = _store(tmp_path)
    data = os.urandom(3 * CS)
    digest = sha256_bytes(data)
    for i, (chunk, hex_) in enumerate(_pieces(data)):
        await store.put_part(
            digest, i, size=len(data), chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
        )
    results = await asyncio.gather(*(store.complete_parts(digest) for _ in range(6)))
    assert sum(r.created for r in results) == 1
    assert all(r.size == len(data) for r in results)
    assert store.path_for(digest).read_bytes() == data
    late = await store.complete_parts(digest)
    assert not late.created


def _manifest_of(root: Path, files: dict[str, bytes]) -> bytes:
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    return build_tree(root).manifest


async def _tree_in(store: BlobStore, root: Path, files: dict[str, bytes]) -> Digest:
    return await _put(store, _manifest_of(root, files))


async def test_tree_members_are_built_once_per_tree(tmp_path: Path) -> None:
    """Review BLOCKING 1: an authorised read is a lookup, never a rebuild."""
    store = _store(tmp_path)
    tree_id = await _tree_in(
        store, tmp_path / "p", {f"f{i}": bytes([i]) for i in range(50)}
    )
    first = await store.tree_members(tree_id)
    for _ in range(200):
        assert await store.tree_members(tree_id) is first  # the same set object
    assert store.trees_parsed == 1 and len(first) == 50
    garbage = await _put(store, b'{"not": "a tree"}')
    for _ in range(3):
        assert await store.tree_members(garbage) == frozenset()
        with pytest.raises(TreeError):
            await store.tree(garbage)
    assert store.trees_parsed == 1  # a non-tree is remembered, not re-parsed


async def test_tree_cache_is_bounded_by_entries(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs", max_blob_bytes=1 << 20, tree_cache_entries=12)
    store.prepare()
    a = await _tree_in(store, tmp_path / "a", {f"a{i}": b"a%d" % i for i in range(10)})
    b = await _tree_in(store, tmp_path / "b", {f"b{i}": b"b%d" % i for i in range(10)})
    await store.tree_members(a)
    await store.tree_members(b)  # 11 + 11 > 12: a is evicted
    await store.tree_members(a)
    assert store.trees_parsed == 3


async def test_abandoned_part_uploads_are_swept_periodically(tmp_path: Path) -> None:
    import os as _os

    mono = [0.0]
    store = BlobStore(
        tmp_path / "blobs",
        max_blob_bytes=1 << 20,
        clock=lambda: mono[0],
        resume_sweep_s=10,
    )
    store.prepare()
    old = sha256_bytes(b"abandoned")
    part, sidecar = store.cas.resume_paths(old)
    part.write_bytes(b"x")
    sidecar.write_bytes(b"{}")
    stale = 1_000.0
    _os.utime(part, (stale, stale))
    _os.utime(sidecar, (stale, stale))
    data = os.urandom(CS)
    chunk, hex_ = _pieces(data)[0]
    mono[0] = 11.0
    await store.put_part(
        sha256_bytes(data), 0, size=CS, chunk_size=CS, chunk_hex=hex_, body=_one(chunk)
    )
    assert store.resumes_swept == 1 and not part.exists() and not sidecar.exists()
