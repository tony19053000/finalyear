"""V0.7 G5 tester round 1 (D145e): more ways the agent's cache folders can be
not ours - each skips seeding and writes nothing."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meshforge.cli.transfer import seed_local
from meshforge.common.cas import CasStore
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX uids"),
]


def _project(tmp_path: Path) -> BuiltTree:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("print('hi')\n")
    return build_tree(project)


def _agent_dir(tmp_path: Path) -> Path:
    data = tmp_path / "agent-data"
    CasStore(data / "cas", max_blob_bytes=1 << 30).prepare()
    return data


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("which", ["sha256", "tmp"])
def test_a_symlinked_inner_cas_folder_is_refused(tmp_path: Path, which: str) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (data / "cas" / which).rename(tmp_path / f"real-{which}")
    (data / "cas" / which).symlink_to(elsewhere)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason == "other_user"
    assert _files(elsewhere) == []


@pytest.mark.parametrize("which", ["cas", "cas/sha256", "cas/tmp"])
def test_one_foreign_folder_in_the_chain_refuses_the_whole_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """Only ONE of the three folders belongs to somebody else."""
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    foreign = (data / which).lstat().st_uid + 1
    real_lstat = Path.lstat

    def lstat(self: Path) -> os.stat_result:
        info = real_lstat(self)
        if self == data / which:
            fields = list(info)
            fields[4] = foreign
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    before = _files(data)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason == "other_user"
    assert _files(data) == before


def test_a_cache_that_is_a_file_not_a_folder_is_not_seeded(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = tmp_path / "agent-data"
    data.mkdir()
    (data / "cas").write_text("not a directory")
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason in ("no_cache", "other_user")
    assert (data / "cas").read_text() == "not a directory"
