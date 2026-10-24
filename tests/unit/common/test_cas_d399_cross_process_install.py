"""D399: a resumable ``complete`` installs the inode it hashed, never the path.

CI run 40109969829 (2-core runner) failed the multi-process hammer
(``test_processes_sharing_one_cas_dir_only_ever_see_typed_errors``): a blob was
present under its digest but did not verify. Root cause: ``complete`` hashed the
part, checked its inode with ``stat``, then renamed the part *by path*. The
digest lock and ``resume_lock`` are per process, so another process sharing the
CAS directory could sweep the part and open a fresh (zero-filled) one at the
same path between the ``stat`` and the rename - and that unhashed file was
installed under the digest.

Two ``CasStore`` objects on one root stand in for two processes (each has its
own locks, exactly like two processes). The other "process" acts inside the
window deterministically, through a hook on the first store's install step.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE, CasError, CasStore
from meshforge.common.hashing import sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE


def _chunks(data: bytes) -> list[bytes]:
    return [data[i : i + CS] for i in range(0, len(data), CS)]


def test_part_swapped_by_another_process_before_the_rename_is_never_installed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cas"
    a = CasStore(root, max_blob_bytes=1 << 30)
    a.prepare()
    b = CasStore(root, max_blob_bytes=1 << 30)  # "another process", own locks
    b.prepare()
    data = os.urandom(3 * CS - 5)
    digest = sha256_bytes(data)

    part = a.resumable(digest, len(data), CS)
    for i, c in enumerate(_chunks(data)):
        part.write_chunk(i, c, sha256_bytes(c).hex)

    real_install = a._install
    fresh: list[object] = []

    def racing_install(src: Path, d: object, size: int) -> bool:
        # The other process, inside the window: sweep everything (TTL 0) and
        # start a new transfer of the same digest - a zero-filled part file at
        # the very path the first process just checked.
        b._resume_ttl_s = 0.0
        b.sweep_resumes()
        fresh.append(b.resumable(digest, len(data), CS))
        return real_install(src, d, size)  # type: ignore[arg-type]

    a._install = racing_install  # type: ignore[method-assign,assignment]
    with contextlib.suppress(CasError):  # typed refusal fine; a wrong blob is not
        part.complete()
    assert fresh, "the race hook did not run"

    final = a.path_for(digest)
    if final.exists():
        assert sha256_bytes(final.read_bytes()) == digest, "unhashed part installed"
    # The other process's fresh transfer still completes into the right blob.
    other = b.resumable(digest, len(data), CS)
    for i, c in enumerate(_chunks(data)):
        other.write_chunk(i, c, sha256_bytes(c).hex)
    other.complete()
    assert b.verify(digest)


def test_a_failed_install_keeps_the_verified_part_for_a_retry(tmp_path: Path) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    data = os.urandom(2 * CS + 7)
    digest = sha256_bytes(data)
    part = store.resumable(digest, len(data), CS)
    for i, c in enumerate(_chunks(data)):
        part.write_chunk(i, c, sha256_bytes(c).hex)
    real_install = store._install

    def failing_install(src: Path, d: object, size: int) -> bool:
        raise PermissionError("injected")

    store._install = failing_install  # type: ignore[method-assign,assignment]
    with pytest.raises(CasError):
        part.complete()
    store._install = real_install  # type: ignore[method-assign]
    assert part.part.exists(), "the verified part was lost"
    assert not [p for p in store.tmp.iterdir() if not p.name.startswith("resume-")]
    assert part.complete().created
    assert store.verify(digest)
    assert store.resumes_in_progress() == 0
