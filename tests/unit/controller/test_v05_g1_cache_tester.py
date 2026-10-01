# ruff: noqa: S311, ASYNC240
"""V0.5 G1 round 3 tester: tree cache bound/parse-once, resumable cap, sweep guard,
disk floor mid-upload. Derived from the review fixes B1/B2, not the code."""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.tree import build_tree
from meshforge.controller.blobs import BlobError, BlobStore

pytestmark = pytest.mark.asyncio
CS = MIN_CHUNK_SIZE


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _store(tmp_path: Path, **kw: object) -> BlobStore:
    store = BlobStore(tmp_path / "blobs", max_blob_bytes=1 << 30, **kw)  # type: ignore[arg-type]
    store.prepare()
    return store


async def _put(store: BlobStore, data: bytes) -> Digest:
    d = sha256_bytes(data)
    await store.put(d, _one(data), declared_size=len(data))
    return d


async def _tree(
    store: BlobStore, root: Path, n: int, salt: str
) -> tuple[Digest, set[str]]:
    root.mkdir()
    members: set[str] = set()
    for i in range(n):
        data = f"{salt}-{i}".encode()
        (root / f"f{i}").write_bytes(data)
        members.add(sha256_bytes(data).hex)
    built = build_tree(root, chunk_size=CS)
    return await _put(store, built.manifest), members


def _piece(data: bytes, k: int) -> tuple[bytes, str]:
    p = data[k * CS : (k + 1) * CS]
    return p, hashlib.sha256(p).hexdigest()


async def _part(store: BlobStore, data: bytes, k: int) -> bool:
    p, h = _piece(data, k)
    return await store.put_part(
        sha256_bytes(data), k, size=len(data), chunk_size=CS, chunk_hex=h, body=_one(p)
    )


# ------------------------------------------------------------------ tree cache


async def test_exactly_one_parse_for_many_concurrent_first_reads(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    tid, members = await _tree(store, tmp_path / "t", 300, "a")
    results = await asyncio.gather(*[store.tree_members(tid) for _ in range(300)])
    assert all(r == members for r in results)
    assert store.trees_parsed == 1
    again = await asyncio.gather(*[store.tree(tid) for _ in range(50)])
    assert all(t is again[0] for t in again) and store.trees_parsed == 1


async def test_non_tree_blobs_are_remembered_empty_and_never_reparsed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    junk = await _put(store, os.urandom(5000))
    bundle = await _put(store, b"\x28\xb5\x2f\xfd" + os.urandom(100))
    bad = await _put(store, b'{"v":1}')
    for d in (junk, bundle, bad):
        res = await asyncio.gather(*[store.tree_members(d) for _ in range(50)])
        assert all(r == frozenset() for r in res)
    assert store.trees_parsed == 0
    assert store._tree_weight == 3  # three negative entries, weight 1 each


async def test_absent_blob_is_not_cached_so_a_later_upload_is_a_tree(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    src = tmp_path / "t"
    src.mkdir()
    (src / "a").write_bytes(b"hello")
    built = build_tree(src, chunk_size=CS)
    tid = built.tree.tree_id
    assert await store.tree_members(tid) == frozenset()  # not stored yet
    assert store._trees == {}
    await _put(store, built.manifest)
    assert await store.tree_members(tid) == {sha256_bytes(b"hello").hex}


async def test_entry_bound_holds_and_answers_stay_correct_under_eviction_churn(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, tree_cache_entries=20)
    trees = [await _tree(store, tmp_path / f"t{i}", 8, f"s{i}") for i in range(12)]
    rng = random.Random(1)

    async def read(i: int) -> None:
        tid, members = trees[i]
        for _ in range(5):
            assert await store.tree_members(tid) == members
            await asyncio.sleep(0)
            assert store._tree_weight <= 20 + 9  # bound, +1 oversize newcomer
            assert store._tree_weight == sum(c.weight for c in store._trees.values())

    await asyncio.gather(*[read(rng.randrange(12)) for _ in range(200)])
    assert store._tree_weight <= 20
    assert store._tree_weight == sum(c.weight for c in store._trees.values())
    assert len(store._trees) <= 2  # 9 per tree: two fit in 20
    assert store._part_locks == {}


async def test_one_tree_larger_than_the_whole_bound_still_works(tmp_path: Path) -> None:
    store = _store(tmp_path, tree_cache_entries=3)
    tid, members = await _tree(store, tmp_path / "t", 30, "x")
    for _ in range(3):
        assert await store.tree_members(tid) == members
    assert store.trees_parsed <= 3  # may re-parse, must never be wrong or loop


async def test_a_reader_holding_a_member_set_survives_its_eviction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, tree_cache_entries=10)
    a, members_a = await _tree(store, tmp_path / "a", 8, "a")
    b, _ = await _tree(store, tmp_path / "b", 8, "b")
    held = await store.tree_members(a)
    await store.tree_members(b)  # evicts a
    assert a.hex not in store._trees
    assert held == members_a  # immutable snapshot, still right


async def test_stored_blob_content_is_immutable_so_the_negative_cache_cannot_lie(
    tmp_path: Path,
) -> None:
    """Re-putting the same digest never changes bytes; a different manifest is a
    different digest. So a remembered non-tree stays a non-tree."""
    store = _store(tmp_path)
    junk = os.urandom(300)
    d = await _put(store, junk)
    assert await store.tree_members(d) == frozenset()
    await store.put(d, _one(junk), declared_size=len(junk))
    assert await store.tree_members(d) == frozenset()
    assert store.trees_parsed == 0


# ------------------------------------------------------ resumable cap (65th)


async def test_the_cap_refuses_new_transfers_and_recovers_after_complete(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    cap = store.cas.max_resumable
    blobs = [os.urandom(2 * CS) + i.to_bytes(4, "big") for i in range(cap + 2)]
    for b in blobs[:cap]:
        assert await _part(store, b, 0) is True
    assert store.cas.resumes_in_progress() == cap
    with pytest.raises(BlobError) as info:
        await _part(store, blobs[cap], 0)
    assert info.value.failure_type is FailureType.RESOURCE_BUSY
    assert store.cas.resumes_in_progress() == cap
    # An existing transfer keeps working at the cap.
    assert await _part(store, blobs[0], 1) is True
    # Finishing one frees a slot.
    assert await _part(store, blobs[0], 2) is True
    await store.complete_parts(sha256_bytes(blobs[0]))
    assert store.cas.resumes_in_progress() == cap - 1
    assert await _part(store, blobs[cap], 0) is True
    with pytest.raises(BlobError):
        await _part(store, blobs[cap + 1], 0)


async def test_the_cap_recovers_when_abandoned_transfers_expire(tmp_path: Path) -> None:
    now = [1000.0]
    store = _store(tmp_path, clock=lambda: now[0], resume_sweep_s=10.0)
    store.cas._wall = lambda: now[0]
    store.cas._resume_ttl_s = 100.0
    cap = store.cas.max_resumable
    blobs = [os.urandom(2 * CS) + i.to_bytes(4, "big") for i in range(cap + 1)]
    for b in blobs[:cap]:
        await _part(store, b, 0)
        for p in store.cas.resume_paths(sha256_bytes(b)):
            os.utime(p, (now[0], now[0]))
    with pytest.raises(BlobError):
        await _part(store, blobs[cap], 0)
    now[0] += 500  # everything is long abandoned; the sweep interval has passed
    assert await _part(store, blobs[cap], 0) is True
    assert store.resumes_swept == cap
    assert store.cas.resumes_in_progress() == 1


async def test_concurrent_new_transfers_cannot_blow_far_past_the_cap(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.cas.max_resumable = 5
    blobs = [os.urandom(2 * CS) + i.to_bytes(4, "big") for i in range(40)]
    res = await asyncio.gather(
        *[_part(store, b, 0) for b in blobs], return_exceptions=True
    )
    ok = sum(1 for r in res if r is True)
    assert all(r is True or isinstance(r, BlobError) for r in res)
    # The cap is soft under concurrency: at most the upload slots (8) may race
    # past it (minor, reported). It must never be unbounded.
    assert store.cas.resumes_in_progress() <= 5 + 8 - 1, ok


async def test_failed_first_chunks_must_not_leak_uncounted_sparse_parts(
    tmp_path: Path,
) -> None:
    """A client whose first chunk fails verification, repeated over many digests,
    must not leave unbounded full-size part files the cap cannot see."""
    store = _store(tmp_path)
    for i in range(100):
        data = os.urandom(2 * CS) + i.to_bytes(4, "big")
        p, _h = _piece(data, 0)
        with pytest.raises(BlobError):
            await store.put_part(
                sha256_bytes(data),
                0,
                size=len(data),
                chunk_size=CS,
                chunk_hex="0" * 64,
                body=_one(p),
            )
    leftovers = [p for p in store.cas.tmp.iterdir() if p.name.startswith("resume-")]
    assert len(leftovers) <= 2 * store.cas.max_resumable, len(leftovers)


# ---------------------------------------------------- sweep never eats live parts


async def test_sweep_keeps_active_transfers_and_removes_only_idle_ones(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.cas._resume_ttl_s = 1.0
    live = os.urandom(3 * CS)
    idle = os.urandom(3 * CS) + b"x"
    await _part(store, live, 0)
    await _part(store, idle, 0)
    old = time.time() - 3600
    for p in store.cas.resume_paths(sha256_bytes(idle)):
        os.utime(p, (old, old))
    assert await asyncio.to_thread(store.cas.sweep_resumes) == 1
    assert all(p.exists() for p in store.cas.resume_paths(sha256_bytes(live)))
    assert not any(p.exists() for p in store.cas.resume_paths(sha256_bytes(idle)))
    # The live one finishes correctly after the sweep.
    await _part(store, live, 1)
    await _part(store, live, 2)
    await store.complete_parts(sha256_bytes(live))
    assert store.path_for(sha256_bytes(live)).read_bytes() == live


async def test_sweep_racing_with_uploads_never_corrupts_or_crashes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.cas._resume_ttl_s = 0.0  # everything not touched this instant is "old"
    blobs = [os.urandom(4 * CS) + i.to_bytes(4, "big") for i in range(6)]

    async def upload(b: bytes) -> None:
        for _attempt in range(30):  # restart-from-scratch after a sweep is legal
            try:
                for k in range(5):
                    await _part(store, b, k)
                await store.complete_parts(sha256_bytes(b))
                return
            except BlobError:
                await asyncio.sleep(0)
        # TTL 0 + a spinning sweeper can starve an upload forever: legal. The
        # test only demands no crash and no wrong blob.

    async def sweeper() -> None:
        for _ in range(200):
            await asyncio.to_thread(store.cas.sweep_resumes)
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(sweeper(), *[upload(b) for b in blobs]), 60)
    for b in blobs:  # whatever happened, a stored blob is exactly right
        if store.size(sha256_bytes(b)) is not None:
            assert store.path_for(sha256_bytes(b)).read_bytes() == b
    assert store._part_locks == {}


# --------------------------------------------------- disk floor mid-upload


async def test_disk_floor_crossed_mid_upload_refuses_and_leaves_no_visible_blob(
    tmp_path: Path,
) -> None:
    free = [10**12]
    floor = 1 << 30
    store = _store(tmp_path, free_disk_floor_bytes=floor, disk_free=lambda _p: free[0])
    chunk = 8 * 1024 * 1024
    n = 40  # 320 MiB blob in 8 MiB chunks (the default chunk size)
    data = os.urandom(chunk) * n
    digest = sha256_bytes(data)
    refused_at = None
    failed: BlobError | None
    for k in range(n):
        if k == 5:
            free[0] = floor  # the disk fills up from other writers
        piece = data[k * chunk : (k + 1) * chunk]
        call = store.put_part(
            digest,
            k,
            size=len(data),
            chunk_size=chunk,
            chunk_hex=hashlib.sha256(piece).hexdigest(),
            body=_one(piece),
        )
        try:
            await call
        except BlobError as exc:
            failed = exc
            refused_at = k
            break
    else:
        failed = None
    if failed is not None:
        assert failed.failure_type is FailureType.DISK_FULL
    assert store.size(digest) is None and not store.cas.has(digest)
    assert not store.path_for(digest).exists()
    assert refused_at is not None, (
        "the free-disk floor was never re-checked during a 320 MiB upload sent as "
        "8 MiB parts (each part request opens a fresh ResumablePart)"
    )
