"""V0.7 G5 item 0, D158 T-4: a per-owner sub-cap under the global resumable cap.

One named uploader at its sub-cap is refused ``resource_busy``; other owners
still start; the shared transfer (owner ``None``) only meets the global cap;
continuing one's own transfer never counts as a new one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meshforge.common.cas import (
    DEFAULT_MAX_RESUMABLE,
    MIN_CHUNK_SIZE,
    CasError,
    CasStore,
    owner_tag,
    resume_key,
)
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE


def _store(tmp_path: Path, **kw: object) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30, **kw)  # type: ignore[arg-type]
    store.prepare()
    return store


def _blob(i: int) -> tuple[Digest, bytes]:
    data = os.urandom(2 * CS) + bytes([i % 256])
    return sha256_bytes(data), data


def _busy(store: CasStore, digest: Digest, size: int, owner: str | None) -> None:
    with pytest.raises(CasError) as info:
        store.resumable(digest, size, CS, owner=owner)
    assert info.value.failure_type is FailureType.RESOURCE_BUSY


def test_owner_at_its_sub_cap_is_busy_and_another_owner_still_starts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_resumable=10, max_resumable_per_owner=3)
    blobs = [_blob(i) for i in range(5)]
    for digest, data in blobs[:3]:
        store.resumable(digest, len(data), CS, owner="user:bob")
    assert store.resumes_of("user:bob") == 3
    _busy(store, blobs[3][0], len(blobs[3][1]), "user:bob")
    # Another owner, same digest or a new one: still starts.
    store.resumable(blobs[3][0], len(blobs[3][1]), CS, owner="user:alice")
    store.resumable(blobs[0][0], len(blobs[0][1]), CS, owner="user:alice")
    assert store.resumes_of("user:alice") == 2
    assert store.resumes_in_progress() == 5


def test_continuing_ones_own_transfer_at_the_sub_cap_is_allowed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_resumable=10, max_resumable_per_owner=2)
    (d1, b1), (d2, b2) = _blob(1), _blob(2)
    store.resumable(d1, len(b1), CS, owner="node:n1")
    store.resumable(d2, len(b2), CS, owner="node:n1")
    again = store.resumable(d1, len(b1), CS, owner="node:n1")
    assert again.count == 3
    assert store.resumes_of("node:n1") == 2


def test_a_finished_or_discarded_transfer_frees_the_owners_slot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_resumable=10, max_resumable_per_owner=1)
    (d1, b1), (d2, b2) = _blob(1), _blob(2)
    part = store.resumable(d1, len(b1), CS, owner="user:bob")
    _busy(store, d2, len(b2), "user:bob")
    part.discard()
    assert store.resumes_of("user:bob") == 0
    store.resumable(d2, len(b2), CS, owner="user:bob")


def test_the_shared_transfer_has_no_sub_cap(tmp_path: Path) -> None:
    store = _store(tmp_path, max_resumable=6, max_resumable_per_owner=2)
    for i in range(6):
        digest, data = _blob(i)
        store.resumable(digest, len(data), CS)
    digest, data = _blob(99)
    _busy(store, digest, len(data), None)  # the global cap still holds


def test_the_global_cap_still_wins_over_free_sub_caps(tmp_path: Path) -> None:
    store = _store(tmp_path, max_resumable=4, max_resumable_per_owner=3)
    for i, owner in enumerate(["user:a", "user:a", "user:b", "user:b"]):
        digest, data = _blob(i)
        store.resumable(digest, len(data), CS, owner=owner)
    digest, data = _blob(9)
    _busy(store, digest, len(data), "user:c")


def test_default_sub_cap_is_the_global_cap_and_bad_values_are_refused(
    tmp_path: Path,
) -> None:
    assert _store(tmp_path).max_resumable_per_owner == DEFAULT_MAX_RESUMABLE
    with pytest.raises(ValueError):
        CasStore(tmp_path / "x", max_blob_bytes=1, max_resumable_per_owner=0)


def test_count_survives_a_restart_by_owner_tag(tmp_path: Path) -> None:
    store = _store(tmp_path, max_resumable=10, max_resumable_per_owner=2)
    for i in range(2):
        digest, data = _blob(i)
        store.resumable(digest, len(data), CS, owner="user:bob")
    again = _store(tmp_path, max_resumable=10, max_resumable_per_owner=2)
    assert again.resumes_of("user:bob") == 2
    digest, data = _blob(7)
    _busy(again, digest, len(data), "user:bob")


def test_owner_tag_is_the_resume_key_suffix() -> None:
    digest, _ = _blob(1)
    assert resume_key(digest, "user:x") == f"{digest.hex}-{owner_tag('user:x')}"
    assert owner_tag("user:x") != owner_tag("user:y")
