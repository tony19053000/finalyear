"""V0.5 G1 D79/D80: the shared content-addressed store (``common/cas.py``)."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from meshforge.common.cas import (
    MIN_CHUNK_SIZE,
    CasError,
    CasFullError,
    CasIntegrityError,
    CasStore,
    CasTooLargeError,
    chunk_count,
    chunk_digests,
    chunk_span,
)
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes

KIB = 1024
CS = MIN_CHUNK_SIZE


def _store(tmp_path: Path, **kw: object) -> CasStore:
    kw.setdefault("max_blob_bytes", 64 * 1024 * 1024)
    store = CasStore(tmp_path / "cas", **kw)  # type: ignore[arg-type]
    store.prepare()
    return store


def _blobs(store: CasStore) -> list[Path]:
    return sorted(p for p in (store.root / "sha256").rglob("*") if p.is_file())


# ----------------------------------------------------------------- layout


def test_put_bytes_uses_the_two_level_layout_and_is_read_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = b"hello cas"
    result = store.put_bytes(data)
    digest = sha256_bytes(data)
    assert result.digest == digest and result.created and result.size == len(data)
    path = store.root / "sha256" / digest.hex[:2] / digest.hex[2:4] / digest.hex
    assert path.read_bytes() == data
    assert store.has(digest) and store.size(digest) == len(data)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) & 0o222 == 0
    assert list(store.tmp.iterdir()) == []
    assert store.used_bytes == len(data)


def test_second_put_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_bytes(b"x")
    again = store.put_bytes(b"x")
    assert not again.created
    assert len(_blobs(store)) == 1 and store.used_bytes == 1


def test_put_with_wrong_digest_keeps_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CasIntegrityError) as info:
        store.put_bytes(b"abc", sha256_bytes(b"abd"))
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert _blobs(store) == [] and list(store.tmp.iterdir()) == []


def test_put_file_hashes_while_copying(tmp_path: Path) -> None:
    store = _store(tmp_path)
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(300 * KIB))
    result = store.put_file(src)
    assert result.digest == sha256_bytes(src.read_bytes())
    assert store.path_for(result.digest).read_bytes() == src.read_bytes()


def test_legacy_v04_layout_is_migrated(tmp_path: Path) -> None:
    root = tmp_path / "cas"
    data = b"legacy"
    digest = sha256_bytes(data)
    legacy = root / "sha256" / digest.hex[:2] / digest.hex
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(data)
    store = CasStore(root, max_blob_bytes=KIB)
    report = store.prepare()
    assert report.migrated == 1 and not legacy.exists()
    assert store.path_for(digest).read_bytes() == data
    assert report.used_bytes == len(data)


def test_prepare_sweeps_one_shot_parts_and_expired_resumes(tmp_path: Path) -> None:
    now = [1_000_000.0]
    store = CasStore(tmp_path / "cas", max_blob_bytes=KIB, wall_clock=lambda: now[0])
    store.prepare()
    (store.tmp / "abc.part").write_bytes(b"half")
    old_part, old_side = store.resume_paths(sha256_bytes(b"old"))
    new_part, new_side = store.resume_paths(sha256_bytes(b"new"))
    for p in (old_part, old_side, new_part, new_side):
        p.write_bytes(b"x")
    os.utime(old_part, (now[0] - 90_000, now[0] - 90_000))
    os.utime(old_side, (now[0] - 90_000, now[0] - 90_000))
    os.utime(new_part, (now[0] - 10, now[0] - 10))
    os.utime(new_side, (now[0] - 10, now[0] - 10))
    report = store.prepare()
    assert report.swept == 1 and report.expired_resumes == 1
    assert sorted(p.name for p in store.tmp.iterdir()) == sorted(
        [new_part.name, new_side.name]
    )


# -------------------------------------------------------------- admission


def test_max_blob_bytes_max_accepted_and_max_plus_one_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path, max_blob_bytes=10)
    assert store.put_bytes(b"a" * 10).created
    with pytest.raises(CasTooLargeError):
        store.put_bytes(b"b" * 11)
    with pytest.raises(CasTooLargeError), store.writer(None) as w:  # unknown size
        w.write(b"c" * 11)
    assert list(store.tmp.iterdir()) == []


def test_quota_max_accepted_and_max_plus_one_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path, quota_bytes=10)
    assert store.put_bytes(b"a" * 6).created
    assert store.put_bytes(b"b" * 4).created  # exactly at the quota
    with pytest.raises(CasFullError) as info:
        store.put_bytes(b"c")  # one byte over
    assert info.value.failure_type is FailureType.DISK_FULL
    assert info.value.details["reason"] == "quota"
    assert store.used_bytes == 10 and store.refused_quota == 1


def test_quota_counts_unknown_size_streams_as_they_grow(tmp_path: Path) -> None:
    store = _store(tmp_path, quota_bytes=5)
    with store.writer(None) as w:
        w.write(b"abc")
        with pytest.raises(CasFullError):
            w.write(b"def")
    assert list(store.tmp.iterdir()) == [] and store.used_bytes == 0


def test_free_disk_floor_max_accepted_and_max_plus_one_rejected(
    tmp_path: Path,
) -> None:
    free = [1000]
    store = _store(tmp_path, free_disk_floor_bytes=900, disk_free=lambda _p: free[0])
    assert store.put_bytes(b"a" * 100).created  # leaves exactly the floor
    with pytest.raises(CasFullError) as info:
        store.put_bytes(b"b" * 101)  # one byte below the floor
    assert info.value.details["reason"] == "insufficient_storage"
    assert store.refused_disk == 1


def test_resumable_open_checks_the_floor(tmp_path: Path) -> None:
    store = _store(tmp_path, free_disk_floor_bytes=10, disk_free=lambda _p: 10 + CS)
    store.resumable(sha256_bytes(b"x" * CS), CS, CS).discard()  # exactly fits
    with pytest.raises(CasFullError):
        store.resumable(sha256_bytes(b"y" * (CS + 1)), CS + 1, CS)


# -------------------------------------------------------------- integrity


def test_verify_detects_on_disk_corruption_and_evicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.put_bytes(b"precious bytes")
    path = store.path_for(result.digest)
    path.chmod(0o600)
    path.write_bytes(b"precious bytez")  # real corruption on disk
    assert store.verify(result.digest) is False
    assert not store.has(result.digest) and store.used_bytes == 0
    assert store.verify(result.digest) is False  # absent


def test_verify_accepts_an_intact_blob(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.put_bytes(b"ok")
    assert store.verify(result.digest) is True


def test_crash_mid_write_leaves_no_visible_blob(tmp_path: Path) -> None:
    """A child process is SIGKILLed while it streams a blob into the store."""
    root = tmp_path / "cas"
    CasStore(root, max_blob_bytes=1 << 30).prepare()
    marker = tmp_path / "started"
    code = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from meshforge.common.cas import CasStore
        store = CasStore(Path({str(root)!r}), max_blob_bytes=1 << 30)
        with store.writer(None, None) as w:
            w.write(b"x" * 65536)
            Path({str(marker)!r}).write_text("1")
            while True:
                w.write(b"y" * 65536)
                time.sleep(0.01)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 30
        while not marker.exists():
            assert child.poll() is None, "child died early"
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        child.kill()
        child.wait(10)
    store = CasStore(root, max_blob_bytes=1 << 30)
    assert [p for p in (root / "sha256").rglob("*") if p.is_file()] == []
    assert len(list(store.tmp.iterdir())) == 1  # the part file, never a blob
    report = store.prepare()
    assert report.swept == 1 and list(store.tmp.iterdir()) == []


# ------------------------------------------------------------------ chunks


def test_chunk_maths() -> None:
    assert chunk_count(0, CS) == 1
    assert chunk_count(CS, CS) == 1
    assert chunk_count(CS + 1, CS) == 2
    assert chunk_span(1, CS + 1, CS) == (CS, 1)
    with pytest.raises(ValueError):
        chunk_span(2, CS + 1, CS)
    with pytest.raises(ValueError):
        chunk_count(10, CS - 1)


@pytest.mark.parametrize("size", [0, 1, CS - 1, CS, CS + 1, 3 * CS])
def test_chunk_digests_match_slices(tmp_path: Path, size: int) -> None:
    data = os.urandom(size)
    path = tmp_path / "f"
    path.write_bytes(data)
    whole, got_size, chunks = chunk_digests(path, CS)
    assert whole == sha256_bytes(data) and got_size == size
    assert len(chunks) == chunk_count(size, CS)
    for i, hex_ in enumerate(chunks):
        start, length = chunk_span(i, size, CS)
        assert hashlib.sha256(data[start : start + length]).hexdigest() == hex_


def _chunked(data: bytes) -> tuple[Digest, list[bytes], list[str]]:
    pieces = [data[i : i + CS] for i in range(0, len(data), CS)] or [b""]
    return (
        sha256_bytes(data),
        pieces,
        [hashlib.sha256(p).hexdigest() for p in pieces],
    )


def test_resumable_assembles_out_of_order_and_resumes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(3 * CS + 7)
    digest, pieces, hexes = _chunked(data)
    part = store.resumable(digest, len(data), CS)
    assert part.missing() == [0, 1, 2, 3]
    assert part.write_chunk(2, pieces[2], hexes[2])
    assert part.write_chunk(0, pieces[0], hexes[0])
    assert not part.write_chunk(0, pieces[0], hexes[0])  # duplicate: no-op
    # A new process continues from the sidecar.
    again = CasStore(store.root, max_blob_bytes=store.max_blob_bytes)
    again.prepare()
    resumed = again.resumable(digest, len(data), CS)
    assert resumed.resumed_chunks == 2 and resumed.missing() == [1, 3]
    for i in resumed.missing():
        resumed.write_chunk(i, pieces[i], hexes[i])
    result = resumed.complete()
    assert result.created and again.path_for(digest).read_bytes() == data
    assert list(again.tmp.iterdir()) == []


def test_resumable_refuses_a_corrupt_chunk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(2 * CS)
    digest, pieces, hexes = _chunked(data)
    part = store.resumable(digest, len(data), CS)
    bad = bytearray(pieces[1])
    bad[5] ^= 0xFF
    with pytest.raises(CasIntegrityError):
        part.write_chunk(1, bytes(bad), hexes[1])
    assert part.missing() == [0, 1]
    with pytest.raises(CasError):
        part.write_chunk(1, pieces[1][:-1], None)  # wrong length


def test_resumable_whole_digest_mismatch_restarts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(2 * CS)
    digest = sha256_bytes(b"something else")
    part = store.resumable(digest, len(data), CS)
    part.write_chunk(0, data[:CS], None)
    part.write_chunk(1, data[CS:], None)
    with pytest.raises(CasIntegrityError):
        part.complete()
    assert not store.has(digest) and list(store.tmp.iterdir()) == []


def test_resumable_with_a_different_shape_starts_over(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(2 * CS)
    digest, pieces, hexes = _chunked(data)
    part = store.resumable(digest, len(data), CS)
    part.write_chunk(0, pieces[0], hexes[0])
    other = store.resumable(digest, len(data), 2 * CS)
    assert other.missing() == [0]


def test_resumable_complete_refuses_missing_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = os.urandom(2 * CS)
    digest, pieces, hexes = _chunked(data)
    part = store.resumable(digest, len(data), CS)
    part.write_chunk(0, pieces[0], hexes[0])
    with pytest.raises(CasError):
        part.complete()
