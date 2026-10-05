"""V0.6 G2 (D109): ``compute run`` seeds the local agent's cache.

Real CasStore and GcIndex on disk (no mocks): blobs land verified under their
digests, the GC index marks them used, every skip reason is reachable, the
size bound is tested at max accepted / max+1, and a file changed after hashing
is never stored under the old name.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from meshforge.cli.transfer import seed_local
from meshforge.common.cas import CasStore
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = pytest.mark.unit


def _project(tmp_path: Path) -> BuiltTree:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("print('hi')\n")
    (project / "data.bin").write_bytes(os.urandom(70_000))
    (project / "same.bin").write_bytes(b"x" * 10)
    (project / "copy.bin").write_bytes(b"x" * 10)  # one blob, two paths
    return build_tree(project)


def _agent_dir(tmp_path: Path, *, gc: bool = True) -> Path:
    data = tmp_path / "agent-data"
    CasStore(data / "cas", max_blob_bytes=1 << 30).prepare()
    if gc:
        index = GcIndex(data / "gc")
        index.open()
        index.close()
    return data


def _total(built: BuiltTree) -> int:
    return sum(built.tree.blob_sizes().values()) + len(built.manifest)


def test_seeds_every_blob_and_the_manifest_verified(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert report.seeded and report.reason is None
    blobs = built.tree.blob_sizes()
    assert report.blobs_written == len(blobs) + 1  # + the manifest
    store = CasStore(data / "cas", max_blob_bytes=1 << 30)
    for hex_ in [*blobs, built.tree.tree_id.hex]:
        path = store.path_for(Digest(hex_))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == hex_
    index = GcIndex(data / "gc")
    index.open()
    try:
        for hex_ in [*blobs, built.tree.tree_id.hex]:
            assert index.last_use("blob", hex_) is not None
    finally:
        index.close()
    again = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert again.seeded and again.blobs_written == 0
    assert again.blobs_present == len(blobs)


def test_size_bound_max_accepted_and_max_plus_one_skipped(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    over = seed_local(built, data, max_bytes=_total(built) - 1, free_disk_floor_bytes=0)
    assert not over.seeded and over.reason == "over_quota"
    exact = seed_local(built, data, max_bytes=_total(built), free_disk_floor_bytes=0)
    assert exact.seeded
    zero = seed_local(built, data, max_bytes=0, free_disk_floor_bytes=0)
    assert not zero.seeded and zero.reason == "over_quota"


def test_no_agent_cache_here(tmp_path: Path) -> None:
    built = _project(tmp_path)
    report = seed_local(
        built, tmp_path / "nothing", max_bytes=1 << 30, free_disk_floor_bytes=0
    )
    assert not report.seeded and report.reason == "no_cache"
    assert not (tmp_path / "nothing").exists()  # nothing created


def test_disk_floor_skips_and_keeps_no_partial_blob(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=1 << 62)
    assert not report.seeded and report.reason == "disk_full"
    assert not list((data / "cas" / "tmp").iterdir())


def test_a_file_changed_after_hashing_is_never_stored(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    (tmp_path / "proj" / "data.bin").write_bytes(os.urandom(70_000))
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason == "changed"
    store = CasStore(data / "cas", max_blob_bytes=1 << 30)
    for hex_ in built.tree.blob_sizes():
        path = store.path_for(Digest(hex_))
        if path.exists():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == hex_


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="POSIX permissions; root ignores them",
)
def test_a_cache_this_user_cannot_write_is_skipped(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    tmp_dir = data / "cas" / "tmp"
    tmp_dir.chmod(0o500)
    try:
        report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    finally:
        tmp_dir.chmod(0o700)
    assert not report.seeded and report.reason == "not_writable"


def test_without_a_gc_index_the_blobs_still_count(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path, gc=False)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert report.seeded and report.reason == "no_gc_index"
    assert not (data / "gc").exists()
