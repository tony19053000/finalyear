"""V0.5 G4 deliverable 7 (G1 reviewer pass-2 nits):

* the resumable-transfer cap counts in memory - it never lists ``tmp/`` on the
  hot path - and stays exact through open / complete / discard / sweep /
  restart (``prepare`` counts what is on disk once);
* the controller's tree member set is built inside the parse thread (the event
  loop never builds it).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE, CasError, CasStore
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.tree import build_tree

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE


def _store(tmp_path: Path, **kw: object) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30, **kw)  # type: ignore[arg-type]
    store.prepare()
    return store


def _blob(i: int) -> tuple[Digest, bytes]:
    data = os.urandom(2 * CS) + bytes([i])
    return sha256_bytes(data), data


def test_counter_tracks_open_complete_discard_without_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, max_resumable=2)
    (d1, b1), (d2, _b2), (d3, _b3) = _blob(1), _blob(2), _blob(3)
    real_iterdir = Path.iterdir

    def no_listing(self: Path) -> object:
        if self == store.tmp:
            raise AssertionError("tmp/ was listed on the hot path")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", no_listing)
    p1 = store.resumable(d1, len(b1), CS)
    p2 = store.resumable(d2, len(b1), CS)
    assert store.resumes_in_progress() == 2
    # max accepted (2), max + 1 refused (resource_busy)
    with pytest.raises(CasError) as info:
        store.resumable(d3, len(b1), CS)
    assert info.value.failure_type is FailureType.RESOURCE_BUSY
    # Re-opening an existing transfer is not a new one.
    store.resumable(d1, len(b1), CS)
    assert store.resumes_in_progress() == 2
    for i in range(p1.count):
        start, length = p1.span(i)
        p1.write_chunk(i, b1[start : start + length], None)
    p1.complete()
    assert store.resumes_in_progress() == 1
    p2.discard()
    assert store.resumes_in_progress() == 0
    store.resumable(d3, len(b1), CS)
    assert store.resumes_in_progress() == 1


def test_prepare_counts_what_is_on_disk_and_sweep_forgets(tmp_path: Path) -> None:
    now = [time.time()]
    store = _store(tmp_path, wall_clock=lambda: now[0])
    (d1, b1), (d2, b2) = _blob(1), _blob(2)
    store.resumable(d1, len(b1), CS)
    store.resumable(d2, len(b2), CS)
    again = _store(tmp_path, wall_clock=lambda: now[0])  # "a restart"
    assert again.resumes_in_progress() == 2
    now[0] += 10 * 24 * 3600  # both idle far past the TTL
    assert again.sweep_resumes() == 2
    assert again.resumes_in_progress() == 0


@pytest.mark.asyncio
async def test_tree_member_set_is_built_in_the_parse_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller import blobs as blobs_module
    from meshforge.controller.blobs import BlobStore

    store = BlobStore(tmp_path / "blobs", max_blob_bytes=1 << 30)
    store.prepare()
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(20):
        (root / f"f{i}").write_bytes(f"file {i}".encode())
    built = build_tree(root, chunk_size=CS)

    async def one(data: bytes) -> AsyncIterator[bytes]:
        yield data

    await store.put(
        built.tree.tree_id, one(built.manifest), declared_size=len(built.manifest)
    )
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = blobs_module._parse_cached

    def spy(*args: object) -> object:
        seen.append(threading.get_ident())
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(blobs_module, "_parse_cached", spy)
    members = await store.tree_members(built.tree.tree_id)
    assert members == frozenset(e.sha256 for e in built.tree.files())
    assert seen and seen[0] != loop_thread
    await asyncio.sleep(0)


def test_a_transfer_swept_between_hash_and_rename_is_never_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 fix round 3: the generation is checked again at the rename, in the
    same lock hold. Here the sweep removes the transfer's sidecar (ending its
    generation) right after the hash; the part file itself - same inode - is
    still there, so only the generation check can refuse the install."""
    store = _store(tmp_path)
    (digest, data) = _blob(7)
    part = store.resumable(digest, len(data), CS)
    for i in range(part.count):
        start, length = part.span(i)
        part.write_chunk(i, data[start : start + length], None)
    real = store._check_disk

    def sweep_sidecar_now(d: object, needed: int) -> None:
        real(d, needed)  # type: ignore[arg-type]
        with store.resume_lock:
            part.sidecar.unlink()
            store.resume_forgotten(digest.hex)

    monkeypatch.setattr(store, "_check_disk", sweep_sidecar_now)
    with pytest.raises(CasError):
        part.complete()
    assert not store.has(digest)


def test_a_stale_owner_is_refused_before_it_hashes_anything(tmp_path: Path) -> None:
    """G4 fix round 3: complete() checks its generation before hashing - a
    stale owner gets the typed "restarted meanwhile" refusal, never an
    integrity verdict about another owner's (partly empty) part."""
    from meshforge.common.cas import CasIntegrityError

    store = _store(tmp_path)
    (digest, data) = _blob(8)
    stale = store.resumable(digest, len(data), CS)
    for i in range(stale.count):
        start, length = stale.span(i)
        stale.write_chunk(i, data[start : start + length], None)
    store.resumable(digest, len(data), CS).discard()
    fresh = store.resumable(digest, len(data), CS)
    with pytest.raises(CasError) as info:
        stale.complete()
    assert not isinstance(info.value, CasIntegrityError)
    assert "restarted" in info.value.message
    assert fresh.missing() == list(range(fresh.count))
    assert store.resumes_in_progress() == 1
