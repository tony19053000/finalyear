"""D157 T-2: per-uploader resumable staging in the CAS (``owner=``)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from meshforge.common.cas import (
    MIN_CHUNK_SIZE,
    CasIntegrityError,
    CasStore,
    resume_key,
)
from meshforge.common.hashing import sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE


def _store(root: Path, **kw: object) -> CasStore:
    store = CasStore(root, max_blob_bytes=1 << 30, **kw)  # type: ignore[arg-type]
    store.prepare()
    return store


def test_resume_key_is_the_hex_alone_or_hex_plus_a_tag() -> None:
    digest = sha256_bytes(b"x")
    assert resume_key(digest) == digest.hex
    alice = resume_key(digest, "user:usr_a")
    assert alice.startswith(digest.hex + "-") and len(alice) == 64 + 1 + 16
    assert alice != resume_key(digest, "user:usr_b")
    assert alice == resume_key(digest, "user:usr_a")  # stable across restarts
    assert "/" not in resume_key(digest, "../../etc")  # owner never in a path


def test_two_owners_assemble_one_digest_independently(tmp_path: Path) -> None:
    store = _store(tmp_path / "cas")
    data = os.urandom(CS * 2)
    digest = sha256_bytes(data)
    good = store.resumable(digest, len(data), CS, owner="user:alice")
    evil = store.resumable(digest, len(data), CS, owner="user:bob")
    junk = b"\x66" * CS
    assert evil.write_chunk(0, junk, sha256_bytes(junk).hex)
    assert good.missing() == [0, 1]  # Bob's part is not in Alice's transfer
    for i in range(2):
        chunk = data[i * CS : (i + 1) * CS]
        assert good.write_chunk(i, chunk, sha256_bytes(chunk).hex)
    assert store.resumes_in_progress() == 2
    assert good.complete().created
    assert store.has(digest)
    assert store.resumes_in_progress() == 1  # Bob's is still his
    evil.write_chunk(1, junk, sha256_bytes(junk).hex)
    with pytest.raises(CasIntegrityError):
        evil.complete()  # his junk fails on its own, the blob stays good
    assert store.resumes_in_progress() == 0
    with store.open_read(digest) as fh:
        assert fh.read() == data


def test_the_shared_transfer_is_unchanged_for_owner_none(tmp_path: Path) -> None:
    store = _store(tmp_path / "cas")
    data = os.urandom(CS)
    digest = sha256_bytes(data)
    part, sidecar = store.resume_paths(digest)
    assert part.name == f"resume-{digest.hex}.part"
    store.resumable(digest, len(data), CS)
    assert sidecar.is_file()
    assert not store.resume_paths(digest, "user:alice")[1].exists()


def test_owned_transfers_are_counted_after_a_restart_and_swept_by_ttl(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cas"
    store = _store(root)
    data = os.urandom(CS * 2)
    digest = sha256_bytes(data)
    store.resumable(digest, len(data), CS, owner="user:alice")
    store.resumable(digest, len(data), CS, owner="node:node_x")
    store.resumable(digest, len(data), CS)
    assert _store(root).resumes_in_progress() == 3  # listed again at start
    later = time.time() + 3 * 24 * 3600
    swept = _store(root, wall_clock=lambda: later)
    assert swept.resumes_in_progress() == 0
    assert not any(p.name.startswith("resume-") for p in (root / "tmp").iterdir())
