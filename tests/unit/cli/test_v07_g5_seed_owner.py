"""V0.7 G5 part 2 (D145e, THREAT_MODEL LF-T1): ``compute run`` never seeds an
agent cache owned by another OS user.

A real ``CasStore`` on disk. "Another user" is this test's uid seen from a
CLI whose effective uid differs (``os.geteuid`` patched - creating files as a
second real user needs root); a symlinked cache folder is refused the same
way. The skip writes nothing, and the same cache owned by us is still seeded.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meshforge.cli import transfer
from meshforge.cli.transfer import seed_local
from meshforge.common.cas import CasStore
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX uids"),
    pytest.mark.security,
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


def test_a_cache_owned_by_another_user_is_not_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    before = _files(data)
    monkeypatch.setattr(transfer.os, "geteuid", lambda: os.getuid() + 1)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason == "other_user"
    assert _files(data) == before


def test_a_root_cli_does_not_seed_a_users_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    if os.getuid() == 0:
        pytest.skip("the test itself runs as root")
    monkeypatch.setattr(transfer.os, "geteuid", lambda: 0)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert report.reason == "other_user"


def test_a_symlinked_cache_folder_is_refused(tmp_path: Path) -> None:
    built = _project(tmp_path)
    real = _agent_dir(tmp_path)
    data = tmp_path / "linked"
    data.mkdir()
    (data / "cas").symlink_to(real / "cas")
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert not report.seeded and report.reason == "other_user"


def test_our_own_cache_is_still_seeded(tmp_path: Path) -> None:
    built = _project(tmp_path)
    data = _agent_dir(tmp_path)
    report = seed_local(built, data, max_bytes=1 << 30, free_disk_floor_bytes=0)
    assert report.seeded and report.blobs_written >= 1
