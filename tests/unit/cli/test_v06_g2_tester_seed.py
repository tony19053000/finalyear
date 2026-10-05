"""V0.6 G2 tester: seeding safety under concurrency and hostile cache state (D109)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
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
    (project / "δata ファイル.bin").write_bytes(os.urandom(120_000))
    for i in range(40):
        (project / f"f{i}.txt").write_text(f"file {i}\n" * 50)
    return build_tree(project)


def _agent_dir(tmp_path: Path) -> Path:
    data = tmp_path / "agent-data"
    CasStore(data / "cas", max_blob_bytes=1 << 30).prepare()
    index = GcIndex(data / "gc")
    index.open()
    index.close()
    return data


def _verify(built: BuiltTree, data: Path) -> None:
    store = CasStore(data / "cas", max_blob_bytes=1 << 30)
    for hex_ in [*built.tree.blob_sizes(), built.tree.tree_id.hex]:
        path = store.path_for(Digest(hex_))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == hex_


def test_eight_concurrent_seeders_of_one_project_leave_a_valid_cache(
    tmp_path: Path,
) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    reports: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            reports.append(
                seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(reports) == 8
    _verify(built, data)
    leftovers = list((data / "cas" / "tmp").iterdir())
    assert leftovers == []


def test_a_locked_gc_index_is_a_note_not_a_failure(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    db = data / "gc" / "gc.sqlite"
    blocker = sqlite3.connect(str(db), timeout=0.1, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    finally:
        blocker.close()
    assert report.seeded  # the blobs are in the cache; only the GC lease is missing
    _verify(built, data)


def test_a_corrupt_gc_index_is_a_note_not_a_failure(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    for f in (data / "gc").iterdir():
        if f.is_file():
            f.write_bytes(b"this is not sqlite" * 50)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert report.seeded
    _verify(built, data)


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, non-root"
)
def test_unwritable_cas_skips_and_writes_nothing(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    for sub in ("tmp", "sha256"):
        (data / "cas" / sub).chmod(0o555)
    try:
        report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    finally:
        for sub in ("tmp", "sha256"):
            (data / "cas" / sub).chmod(0o755)
    assert not report.seeded and report.reason == "not_writable"
    assert list((data / "cas" / "sha256").rglob("*")) == []


def test_free_disk_floor_above_what_exists_skips_disk_full(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=1 << 60)
    assert not report.seeded and report.reason == "disk_full"
    assert list((data / "cas" / "tmp").iterdir()) == []


def test_no_cache_dir_at_all_is_a_skip_and_creates_nothing(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = tmp_path / "never-had-an-agent"
    data.mkdir()
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason == "no_cache"
    assert list(data.iterdir()) == []


def test_a_cas_root_that_is_a_file_is_a_skip_not_a_crash(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = tmp_path / "d"
    data.mkdir()
    (data / "cas").write_text("not a directory")
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded


def test_zero_max_bytes_skips_over_quota(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    report = seed_local(built, data, max_bytes=0, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason == "over_quota"
    assert list((data / "cas" / "sha256").rglob("*")) == []
