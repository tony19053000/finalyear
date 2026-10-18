"""V0.9 G3 lane A item 0.1 (D225.2): ``build_job`` onto an existing empty folder
with the **real** ``os.rename`` of the host.

On Linux this is the one-step POSIX rename; on Windows it is the rename-aside
branch (Windows refuses to rename onto an existing folder). The unit tests cover
the Windows branch with a monkeypatched rename; this file is what the one
Windows lab run of ``tests/component/bench`` proves for real
(``lab/checklists/V0.9-campaign.md`` P1).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
from meshforge.bench.workloads import build_job, get_family
from meshforge.common.manifest import load_manifest

pytestmark = pytest.mark.component

FAM = "cpu.sort"


def test_build_onto_an_existing_empty_folder_with_the_host_rename(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "j"
    dest.mkdir()
    job = build_job(FAM, get_family(FAM).smallest_params(), 1, dest)
    load_manifest(dest)
    assert {
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()
    } == set(job.files)
    # no staging folder and no aside folder left next to it
    assert [p.name for p in tmp_path.iterdir()] == ["j"], sys.platform


def test_repeated_builds_into_fresh_empty_folders_leave_nothing_behind(
    tmp_path: Path,
) -> None:
    for i in range(5):
        dest = tmp_path / f"j{i}"
        dest.mkdir()
        build_job(FAM, get_family(FAM).smallest_params(), i, dest)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"j{i}" for i in range(5)]
