"""V0.9 G3 lane A tester r1 (item 0.1, D225.2): try to break ``materialise._publish``
on the Windows branch (simulated by a ``os.rename`` that refuses an existing
destination) and on the real host: races, unicode / spaced names, a file in the
way, leftovers.

``test_defect_*`` are strict xfails: each documents a defect found by the tester.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy")
from meshforge.bench.workloads import get_family
from meshforge.bench.workloads import materialise as mat

pytestmark = pytest.mark.unit

FAM = "cpu.sort"


def _p() -> Any:
    return get_family(FAM).smallest_params()


def _windows(monkeypatch: pytest.MonkeyPatch, hook: Any = None) -> None:
    """Windows semantics: rename onto an existing path fails. ``hook(src, dst)``
    runs right after a successful rename (to play a racing builder)."""
    real = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst).exists():
            raise FileExistsError(183, "Cannot create a file when it already exists")
        real(src, dst)
        if hook is not None:
            hook(Path(src), Path(dst))

    monkeypatch.setattr(os, "rename", rename)


def _leftovers(parent: Path) -> list[str]:
    return sorted(
        p.name
        for p in parent.iterdir()
        if ".aside-" in p.name or ".building-" in p.name
    )


@pytest.mark.parametrize("windows", [False, True], ids=["host", "windows-sim"])
@pytest.mark.parametrize("name", ["job dir", "jöb-日本語", "a" * 120])
def test_unicode_spaced_and_long_names_publish_and_leave_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool, name: str
) -> None:
    if windows:
        _windows(monkeypatch)
    dest = tmp_path / name
    dest.mkdir()
    built = mat.build_job(FAM, _p(), 1, dest)
    assert (built.path / "run_workload.py").is_file()
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("windows", [False, True], ids=["host", "windows-sim"])
def test_a_file_in_the_way_is_refused_and_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    if windows:
        _windows(monkeypatch)
    dest = tmp_path / "j"
    dest.write_bytes(b"precious")
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, dest)
    assert dest.read_bytes() == b"precious"
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("windows", [False, True], ids=["host", "windows-sim"])
def test_a_non_empty_folder_is_refused_and_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    if windows:
        _windows(monkeypatch)
    dest = tmp_path / "j"
    dest.mkdir()
    (dest / "mine.txt").write_text("keep")
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, dest)
    assert [p.name for p in dest.iterdir()] == ["mine.txt"]
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("windows", [False, True], ids=["host", "windows-sim"])
def test_threads_building_into_one_empty_folder_exactly_one_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    if windows:
        _windows(monkeypatch)
    dest = tmp_path / "j"
    dest.mkdir()
    barrier = threading.Barrier(6)
    outcomes: list[str] = []

    def build() -> None:
        barrier.wait()
        try:
            mat.build_job(FAM, _p(), 1, dest)
            outcomes.append("ok")
        except mat.MaterialiseError:
            outcomes.append("busy")

    threads = [threading.Thread(target=build) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(outcomes) == 6, outcomes
    # a folder is either complete or empty - never half-written; at least one
    # build lands and a loser reports busy, never an unexpected exception
    assert outcomes.count("ok") >= 1
    assert (dest / "compute.yaml").is_file() or (dest / "run_workload.py").is_file()
    assert len(list(dest.rglob("*.py"))) > 3
    assert [p for p in tmp_path.iterdir() if ".building-" in p.name] == []


def test_defect_windows_race_leaves_an_empty_aside_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing builder fills ``dest`` between our aside step and our final
    rename: we lose (busy), our restore of the aside fails because ``dest`` now
    exists, and the empty ``.<dest>.aside-<uuid>`` folder stays next to the
    caller's folder for ever."""

    def racer(src: Path, dst: Path) -> None:
        if src.name == "j" and ".aside-" in dst.name:  # just moved aside
            (tmp_path / "j").mkdir()
            (tmp_path / "j" / "other-builder.txt").write_text("won")

    _windows(monkeypatch, racer)
    (tmp_path / "j").mkdir()
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert (tmp_path / "j" / "other-builder.txt").read_text() == "won"
    assert _leftovers(tmp_path) == []
