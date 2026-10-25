"""D399: a resumable ``complete`` installs exactly the bytes it hashed.

CI run 40109969829 (2-core runner) failed the multi-process hammer
(``test_processes_sharing_one_cas_dir_only_ever_see_typed_errors``): a blob was
present under its digest but did not verify. Root cause: ``complete`` hashed the
part, checked its inode, then renamed the part *by path*. The digest lock,
``resume_lock`` and the generation table are per process, so another process
sharing the CAS directory could sweep the part and open a fresh (zero-filled)
one at the same path inside that window - and that unhashed file was installed.
An inode check alone cannot close it (inode numbers are reused after unlink).

Now ``complete`` moves the part to a private one-shot name first, then hashes
and installs that same file. Two ``CasStore`` objects on one root stand in for
two processes (each has its own locks and generation books, exactly like two
processes); the other "process" acts at chosen points through hooks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import structlog

from meshforge.common.cas import (
    MIN_CHUNK_SIZE,
    CasError,
    CasIntegrityError,
    CasStore,
    ResumablePart,
)
from meshforge.common.hashing import Digest, sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE


def _chunks(data: bytes) -> list[bytes]:
    return [data[i : i + CS] for i in range(0, len(data), CS)]


def _two_stores(tmp_path: Path) -> tuple[CasStore, CasStore]:
    root = tmp_path / "cas"
    a = CasStore(root, max_blob_bytes=1 << 30)
    a.prepare()
    b = CasStore(root, max_blob_bytes=1 << 30)  # "another process", own locks
    b.prepare()
    return a, b


def _filled(store: CasStore, data: bytes) -> ResumablePart:
    part = store.resumable(sha256_bytes(data), len(data), CS)
    for i, c in enumerate(_chunks(data)):
        part.write_chunk(i, c, sha256_bytes(c).hex)
    return part


def _fill(part: ResumablePart, data: bytes) -> None:
    for i, c in enumerate(_chunks(data)):
        part.write_chunk(i, c, sha256_bytes(c).hex)


def _sweep_and_reopen(store: CasStore, digest: Digest, size: int) -> ResumablePart:
    """The other process: sweep everything (TTL 0), then start a new transfer
    of the same digest - a zero-filled part at the very same path."""
    store._resume_ttl_s = 0.0
    store.sweep_resumes()
    store._resume_ttl_s = 600.0
    return store.resumable(digest, size, CS)


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _stray_one_shots(store: CasStore) -> list[str]:
    return [p.name for p in store.tmp.iterdir() if not p.name.startswith("resume-")]


def test_a_part_swapped_in_after_the_move_is_not_installed_and_left_alone(
    tmp_path: Path,
) -> None:
    """The CI race: the other process sweeps + reopens right before the install.
    The moved (hashed) file is installed; the other process's fresh part stays
    where it is and completes into the same blob."""
    a, b = _two_stores(tmp_path)
    data = os.urandom(3 * CS - 5)
    digest = sha256_bytes(data)
    part = _filled(a, data)
    real_install = a._install
    fresh: list[ResumablePart] = []

    def racing_install(src: Path, d: Digest, size: int) -> bool:
        fresh.append(_sweep_and_reopen(b, digest, len(data)))
        return real_install(src, d, size)

    a._install = racing_install  # type: ignore[method-assign,assignment]
    result = part.complete()
    assert result.created
    assert sha256_bytes(a.path_for(digest).read_bytes()) == digest
    (other,) = fresh
    assert other.part.exists()  # B's part is not touched by A
    # The sidecar name is shared on disk, so A's cleanup removes it; B's next
    # chunk rewrites it from B's in-memory bitmap (at worst B re-fetches).
    _fill(other, data)
    assert not other.complete().created  # same name, same bytes
    assert b.verify(digest)
    assert _stray_one_shots(a) == []


def test_a_foreign_part_with_a_reused_inode_number_is_never_installed(
    tmp_path: Path,
) -> None:
    """Inode numbers are reused after an unlink, and A's generation books never
    see B's restart: A's checks all pass for B's fresh zero-filled part. Only
    hashing the moved file catches it: a typed integrity error, no blob."""
    a, b = _two_stores(tmp_path)
    data = os.urandom(3 * CS - 5)
    digest = sha256_bytes(data)
    part = _filled(a, data)
    other = _sweep_and_reopen(b, digest, len(data))
    part._identity = _identity(other.part)  # the reused inode number
    with pytest.raises(CasIntegrityError):
        part.complete()
    assert not a.path_for(digest).exists()
    assert not a.has(digest)
    assert a.resumes_in_progress() == 0
    assert _stray_one_shots(a) == []
    # B is told to start over (typed), and a fresh transfer installs the blob.
    with pytest.raises(CasError):
        _fill(other, data)
    again = b.resumable(digest, len(data), CS)
    _fill(again, data)
    assert again.complete().created
    assert b.verify(digest)


def test_a_foreign_part_with_another_inode_is_handed_back_untouched(
    tmp_path: Path,
) -> None:
    a, b = _two_stores(tmp_path)
    data = os.urandom(2 * CS + 9)
    digest = sha256_bytes(data)
    part = _filled(a, data)
    keep = tmp_path / "keep"
    os.link(part.part, keep)  # keeps A's inode allocated: B's gets another number
    other = _sweep_and_reopen(b, digest, len(data))
    before = _identity(other.part)
    assert before != part._identity
    with pytest.raises(CasError) as info:
        part.complete()
    assert not isinstance(info.value, CasIntegrityError)
    assert not a.has(digest)
    assert _identity(other.part) == before  # B's part is back at its path
    assert _stray_one_shots(a) == []
    _fill(other, data)
    assert other.complete().created
    assert b.verify(digest)


def test_the_private_name_swept_by_prepare_is_a_typed_start_over(
    tmp_path: Path,
) -> None:
    a, b = _two_stores(tmp_path)
    data = os.urandom(2 * CS + 1)
    digest = sha256_bytes(data)
    part = _filled(a, data)
    real_install = a._install

    def install_after_prepare(src: Path, d: Digest, size: int) -> bool:
        b.prepare()  # another process starting: sweeps one-shot *.part files
        return real_install(src, d, size)

    a._install = install_after_prepare  # type: ignore[method-assign,assignment]
    with pytest.raises(CasError) as info:
        part.complete()
    assert not isinstance(info.value, CasIntegrityError)
    assert not a.has(digest)
    assert a.resumes_in_progress() == 0
    a._install = real_install  # type: ignore[method-assign]
    again = a.resumable(digest, len(data), CS)
    assert again.missing() == list(range(again.count))
    _fill(again, data)
    assert again.complete().created
    assert a.verify(digest)


def test_a_failed_install_keeps_the_verified_part_for_a_retry(tmp_path: Path) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    data = os.urandom(2 * CS + 7)
    digest = sha256_bytes(data)
    part = _filled(store, data)
    real_install = store._install

    def failing_install(src: Path, d: Digest, size: int) -> bool:
        raise PermissionError("injected")

    store._install = failing_install  # type: ignore[method-assign,assignment]
    with pytest.raises(CasError):
        part.complete()
    store._install = real_install  # type: ignore[method-assign]
    assert part.part.exists(), "the verified part was lost"
    assert _stray_one_shots(store) == []
    assert part.complete().created
    assert store.verify(digest)
    assert store.resumes_in_progress() == 0


def test_a_restore_that_cannot_link_back_is_logged_and_leaves_no_stray(
    tmp_path: Path,
) -> None:
    a, b = _two_stores(tmp_path)
    data = os.urandom(2 * CS + 3)
    digest = sha256_bytes(data)
    part = _filled(a, data)

    def failing_install(src: Path, d: Digest, size: int) -> bool:
        _sweep_and_reopen(b, digest, len(data))  # the path is taken again
        raise PermissionError("injected")

    a._install = failing_install  # type: ignore[method-assign,assignment]
    with structlog.testing.capture_logs() as logs, pytest.raises(CasError):
        part.complete()
    events = [e for e in logs if e["event"] == "cas.resume_restore_failed"]
    assert len(events) == 1
    assert events[0]["digest"] == str(digest)
    assert events[0]["error_type"] == "FileExistsError"
    assert _stray_one_shots(a) == []
    assert not a.has(digest)
