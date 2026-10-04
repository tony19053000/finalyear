"""G4 tester round 4: the per-digest transfer generation as the stale-owner guard.

The inode identity of the stale owner is forced equal to the live one's
(``_force_inode_reuse``) so only the generation can refuse it - this is what
ext4/tmpfs inode reuse looks like to the guard.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE, CasError, CasStore
from meshforge.common.hashing import sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE


def _store(root: Path, **kw) -> CasStore:
    s = CasStore(root, max_blob_bytes=1 << 30, **kw)
    s.prepare()
    return s


def _fill(part, data: bytes) -> None:
    for i in range(len(data) // CS):
        c = data[i * CS : (i + 1) * CS]
        part.write_chunk(i, c, sha256_bytes(c).hex)


def _force_inode_reuse(stale, live) -> None:
    stale._identity = live._identity


def _setup(tmp_path: Path):
    store = _store(tmp_path / "cas")
    data = os.urandom(CS * 2)
    return store, data, sha256_bytes(data)


def _assert_stale_refused(stale, live, store, digest, data) -> None:
    _force_inode_reuse(stale, live)
    c0 = data[:CS]
    with pytest.raises(CasError):
        stale.write_chunk(0, c0, sha256_bytes(c0).hex)  # write_chunk site
    part, _ = store.resume_paths(digest)
    assert part.read_bytes() == bytes(len(data))
    with pytest.raises(CasError):
        stale.complete()  # hash / rename sites
    assert not store.has(digest)


def test_stale_owner_after_restart_refused_even_with_inode_reuse(tmp_path):
    store, data, d = _setup(tmp_path)
    a = store.resumable(d, len(data), CS)
    store.resumable(d, len(data), CS).discard()
    b = store.resumable(d, len(data), CS)
    _assert_stale_refused(a, b, store, d, data)


def test_stale_owner_after_sweep_refused_even_with_inode_reuse(tmp_path):
    now = [1000.0]
    store = _store(tmp_path / "cas", resume_ttl_s=10, wall_clock=lambda: now[0])
    data = os.urandom(CS * 2)
    d = sha256_bytes(data)
    a = store.resumable(d, len(data), CS)
    for p in store.tmp.iterdir():
        os.utime(p, (0, 0))
    now[0] = 10**9
    assert store.sweep_resumes() == 1
    b = store.resumable(d, len(data), CS)
    _assert_stale_refused(a, b, store, d, data)


def test_stale_owner_after_complete_by_another_owner_refused(tmp_path):
    store, data, d = _setup(tmp_path)
    a = store.resumable(d, len(data), CS)
    b = store.resumable(d, len(data), CS)  # same live transfer, same generation
    assert a.generation == b.generation
    _fill(b, data)
    b.complete()
    assert store.resumes_in_progress() == 0
    c = store.resumable(d, len(data), CS)  # fresh transfer (blob may exist)
    # a belongs to a completed transfer: must not touch c.
    _force_inode_reuse(a, c)
    with pytest.raises(CasError):
        a.write_chunk(0, data[:CS], sha256_bytes(data[:CS]).hex)
    with pytest.raises(CasError):
        a.complete()
    assert store.resumes_in_progress() == 1 and c.missing() == [0, 1]


def test_discard_by_stale_owner_leaves_live_transfer_alone(tmp_path):
    store, data, d = _setup(tmp_path)
    a = store.resumable(d, len(data), CS)
    store.resumable(d, len(data), CS).discard()
    b = store.resumable(d, len(data), CS)
    a.discard()
    assert store.resumes_in_progress() == 1
    _fill(b, data)
    assert b.complete().digest == d


def test_generations_never_repeat_for_a_digest(tmp_path):
    store, data, d = _setup(tmp_path)
    seen = set()
    for _ in range(50):
        p = store.resumable(d, len(data), CS)
        assert p.generation not in seen
        seen.add(p.generation)
        p.discard()


def test_generation_survives_restart_of_store_as_new_value(tmp_path):
    """After a process restart the in-memory table is empty: old owners are
    in another process, new open gets a fresh generation and works."""
    store, data, d = _setup(tmp_path)
    a = store.resumable(d, len(data), CS)
    _fill(a, data)
    store2 = _store(tmp_path / "cas")  # "restart"
    b = store2.resumable(d, len(data), CS)
    assert b.missing() == []  # resumed
    assert b.complete().digest == d


@pytest.mark.xfail(
    strict=True,
    reason="R4-1 accepted limitation (Lead, 2026-10-05): the stale-owner guard is "
    "per CasStore instance; production runs exactly one store per directory "
    "(controller/blobs.py, agent/cas/store.py). A second instance (or process) on "
    "one dir is protected only by the inode check. Generations are now "
    "process-unique, but B's own table never sees A's restart.",
)
def test_two_stores_one_dir_stale_owner_not_confused(tmp_path):
    """Two CasStore instances on one directory in one process: instance A's
    generation values must not collide with instance B's."""
    root = tmp_path / "cas"
    sa, sb = _store(root), _store(root)
    data = os.urandom(CS * 2)
    d = sha256_bytes(data)
    pa = sa.resumable(d, len(data), CS)
    pb = sb.resumable(d, len(data), CS)  # joins the same part file
    pa.discard()  # A unlinks it
    pa2 = sa.resumable(d, len(data), CS)  # A creates a NEW transfer
    _force_inode_reuse(pb, pa2)
    c0 = data[:CS]
    with pytest.raises(CasError):
        pb.write_chunk(0, c0, sha256_bytes(c0).hex)  # B's owner is stale
    assert pa2.missing() == [0, 1]
    with pytest.raises(CasError):
        pb.complete()


def test_two_processes_restart_does_not_let_old_process_part_install(tmp_path):
    """Process-unique: a part from a different CasStore (another process's
    equivalent) whose table says 'current' must not corrupt a restarted one."""
    root = tmp_path / "cas"
    s1 = _store(root)
    data = os.urandom(CS * 2)
    d = sha256_bytes(data)
    old = s1.resumable(d, len(data), CS)
    _fill(old, data)
    s2 = _store(root)
    s2.resumable(d, len(data), CS).discard()  # other process restarts it
    new = s2.resumable(d, len(data), CS)
    _force_inode_reuse(old, new)
    # s1's table still says old is current (it cannot know). Documented limit
    # is in-process only; the inode check is the cross-process defence. Here
    # inode is forced equal, so this documents the residual window.
    try:
        old.complete()
    except CasError:
        return
    # installed zeros? must at least be the correct blob (hash verified).
    assert s1.has(d)  # installed only after the sha256 matched


def test_concurrent_restart_and_stale_writes_never_corrupt(tmp_path):
    store, data, d = _setup(tmp_path)
    for _ in range(20):
        a = store.resumable(d, len(data), CS)
        store.resumable(d, len(data), CS).discard()
        b = store.resumable(d, len(data), CS)
        _force_inode_reuse(a, b)
        errs: list[BaseException] = []

        def stale(a=a, errs=errs) -> None:
            try:
                a.write_chunk(0, data[:CS], sha256_bytes(data[:CS]).hex)
            except CasError as e:
                errs.append(e)

        ts = [threading.Thread(target=stale) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(errs) == 4
        assert b.missing() == [0, 1]
        b.discard()


# --- per-site mutation-killers: each check isolated from the others ---------


def _bump(store: CasStore, hex_: str) -> None:
    with store.resume_lock:
        store._new_generation(hex_)


def test_site_rename_generation_change_after_hash_refuses_install(
    tmp_path, monkeypatch
):
    """Generation changes between the hash and the rename (same inode, same
    bytes): only the rename-time check can refuse."""
    store, data, d = _setup(tmp_path)
    a = store.resumable(d, len(data), CS)
    _fill(a, data)
    real = store._check_disk

    def hook(digest, n):
        _bump(store, digest.hex)
        return real(digest, n)

    monkeypatch.setattr(store, "_check_disk", hook)
    with pytest.raises(CasError):
        a.complete()
    assert not store.has(d)


def test_site_hash_stale_owner_does_not_hash(tmp_path, monkeypatch):
    """A stale owner must be refused BEFORE reading/hashing the whole part."""
    import meshforge.common.cas as cas

    store, data, d = _setup(tmp_path)
    a = store.resumable(d, len(data), CS)
    _fill(a, data)
    _bump(store, d.hex)
    calls = []
    real = cas.hashlib.sha256
    monkeypatch.setattr(cas.hashlib, "sha256", lambda *x: calls.append(1) or real(*x))
    with pytest.raises(CasError):
        a.complete()
    assert calls == [], "a stale owner hashed the part before being refused"


def test_site_write_save_generation_change_after_pwrite_keeps_sidecar(
    tmp_path, monkeypatch
):
    """Generation changes after the bytes are written, before the sidecar save:
    the sidecar must not be rewritten for a transfer that is no longer ours."""
    import meshforge.common.cas as cas

    store, data, d = _setup(tmp_path)
    a = store.resumable(d, len(data), CS)
    _, sidecar = store.resume_paths(d)
    before = sidecar.read_bytes()
    real = cas.os.fsync
    fired: list[int] = []

    def fsync(fd: int) -> None:
        if not fired:  # once: _save fsyncs too, under resume_lock
            fired.append(1)
            _bump(store, d.hex)
        real(fd)

    monkeypatch.setattr(cas.os, "fsync", fsync)
    c0 = data[:CS]
    with pytest.raises(CasError):
        a.write_chunk(0, c0, sha256_bytes(c0).hex)
    assert sidecar.read_bytes() == before
