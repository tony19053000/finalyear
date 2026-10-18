"""V0.9 G3 lane A item 0.1 (D225.2): the Windows rename-aside branch of
``materialise._publish`` uses a ``uuid4`` name and never loses the caller's folder.

Linux renames onto an empty folder, so the Windows branch only runs here with a
monkeypatched ``os.rename`` that refuses an existing destination (what Windows
does). One real Windows run of ``tests/component/bench`` is the lab step.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy")
from meshforge.bench.workloads import get_family
from meshforge.bench.workloads import materialise as mat

pytestmark = pytest.mark.unit

FAM = "cpu.sort"
ASIDE = re.compile(r"^\.j\.aside-[0-9a-f]{32}$")


def _p() -> Any:
    return get_family(FAM).smallest_params()


def _windows_rename(
    monkeypatch: pytest.MonkeyPatch, seen: list[tuple[str, str]], *, fail_final: bool
) -> None:
    """Windows semantics: renaming onto an existing path fails. With
    ``fail_final`` the staging -> dest rename after the aside step fails too."""
    real = os.rename

    def rename(src: Any, dst: Any) -> None:
        seen.append((Path(src).name, Path(dst).name))
        if Path(dst).exists():
            raise FileExistsError(183, "Cannot create a file when that file already exists")
        if fail_final and Path(src).name.startswith(".j.building-") and len(seen) > 1:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(os, "rename", rename)


def test_windows_branch_moves_dest_aside_under_a_uuid4_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    _windows_rename(monkeypatch, seen, fail_final=False)
    (tmp_path / "j").mkdir()
    job = mat.build_job(FAM, _p(), 1, tmp_path / "j")
    asides = [dst for src, dst in seen if src == "j"]
    assert len(asides) == 1 and ASIDE.match(asides[0]), seen
    assert (job.path / "run_workload.py").is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["j"]


def test_two_builds_never_reuse_an_aside_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    _windows_rename(monkeypatch, seen, fail_final=False)
    names = []
    for sub in ("a", "b"):
        parent = tmp_path / sub
        (parent / "j").mkdir(parents=True)
        mat.build_job(FAM, _p(), 1, parent / "j")
        names += [dst for src, dst in seen if src == "j"]
        seen.clear()
    assert len(names) == 2 and names[0] != names[1]


def test_final_rename_failure_restores_the_callers_empty_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    _windows_rename(monkeypatch, seen, fail_final=True)
    (tmp_path / "j").mkdir()
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, tmp_path / "j")
    # dest went aside, the final rename failed, dest came back: nothing lost,
    # no aside and no staging left behind
    assert any(src == "j" and ASIDE.match(dst) for src, dst in seen), seen
    assert any(ASIDE.match(src) and dst == "j" for src, dst in seen), seen
    assert sorted(p.name for p in tmp_path.iterdir()) == ["j"]
    assert list((tmp_path / "j").iterdir()) == []


def test_a_stale_aside_from_a_crashed_build_does_not_block_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old pid/id() name could collide with a crash leftover; uuid4 cannot."""
    seen: list[tuple[str, str]] = []
    _windows_rename(monkeypatch, seen, fail_final=False)
    stale = tmp_path / f".j.aside-{os.getpid()}-0"
    stale.mkdir()
    (tmp_path / "j").mkdir()
    mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert (tmp_path / "j" / "run_workload.py").is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["j", stale.name])
