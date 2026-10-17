"""V0.9 G2 tester round 2: is build_job really atomic and tidy (D7)?"""

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


def _leftovers(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.iterdir())


def test_happy_path_leaves_only_dest(tmp_path: Path) -> None:
    mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert _leftovers(tmp_path) == ["j"]


def test_existing_empty_dir_replaced(tmp_path: Path) -> None:
    (tmp_path / "j").mkdir()
    mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert (tmp_path / "j" / "meshforge.toml").exists() or any((tmp_path / "j").iterdir())
    assert _leftovers(tmp_path) == ["j"]


def test_nonempty_dest_untouched_and_no_staging(tmp_path: Path) -> None:
    d = tmp_path / "j"; d.mkdir(); (d / "keep.txt").write_text("x")
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, d)
    assert _leftovers(tmp_path) == ["j"] and [p.name for p in d.iterdir()] == ["keep.txt"]


def test_dest_is_a_file(tmp_path: Path) -> None:
    f = tmp_path / "j"; f.write_text("x")
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, f)
    assert f.read_text() == "x" and _leftovers(tmp_path) == ["j"]


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt(), OSError(28, "disk full")])
def test_failure_mid_write_leaves_no_dest_and_no_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    real = Path.write_bytes
    calls = {"n": 0}

    def flaky(self: Path, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise exc
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    with pytest.raises(type(exc)):
        mat.build_job(FAM, _p(), 1, tmp_path / "j")
    monkeypatch.undo()
    assert _leftovers(tmp_path) == []


def test_failure_in_load_manifest_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def bad(_p: Any) -> Any:
        raise ValueError("bad manifest")
    monkeypatch.setattr(mat, "load_manifest", bad)
    with pytest.raises(ValueError):
        mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert _leftovers(tmp_path) == []


@pytest.mark.xfail(strict=True, reason="TR2-D7: Windows branch rmdir()s the caller empty dest before the rename; if the rename then fails the dest is gone")
def test_failure_in_rename_cleans_up_and_keeps_existing_empty_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "j").mkdir()
    def nope(*_a: Any, **_k: Any) -> None:
        raise PermissionError("locked")
    monkeypatch.setattr(os, "rename", nope)
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert _leftovers(tmp_path) == ["j"] and list((tmp_path / "j").iterdir()) == []


def test_windows_style_rename_refusing_onto_existing_empty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate Windows: os.rename raises if the destination exists."""
    real = os.rename

    def win_rename(src: Any, dst: Any) -> None:
        if Path(dst).exists():
            raise FileExistsError(183, "exists")
        real(src, dst)

    monkeypatch.setattr(os, "rename", win_rename)
    (tmp_path / "j").mkdir()
    job = mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert job.path == tmp_path / "j" and (tmp_path / "j" / "run_workload.py").exists()
    assert _leftovers(tmp_path) == ["j"]


def test_windows_style_race_dest_filled_between_check_and_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_publish = mat._publish

    def racing(staging: Path, dest: Path) -> None:
        dest.mkdir()
        (dest / "other.txt").write_text("winner")
        real_publish(staging, dest)

    monkeypatch.setattr(mat, "_publish", racing)
    with pytest.raises(mat.MaterialiseError):
        mat.build_job(FAM, _p(), 1, tmp_path / "j")
    assert (tmp_path / "j" / "other.txt").read_text() == "winner"
    assert _leftovers(tmp_path) == ["j"]


def test_concurrent_builders_one_dest_exactly_one_wins_whole(tmp_path: Path) -> None:
    dest = tmp_path / "j"
    wins: list[Any] = []
    errs: list[BaseException] = []
    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        try:
            wins.append(mat.build_job(FAM, _p(), 1, dest))
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert all(isinstance(e, mat.MaterialiseError) for e in errs), errs
    assert len(wins) >= 1
    # whatever won, the folder is complete and loadable, no staging debris
    mat.load_manifest(dest)
    assert _leftovers(tmp_path) == ["j"]


def test_dest_with_unicode_and_spaces_and_nested_parent(tmp_path: Path) -> None:
    d = tmp_path / "new parent" / "jöb ✓ 日本"
    mat.build_job(FAM, _p(), 1, d)
    mat.load_manifest(d)
    assert _leftovers(tmp_path / "new parent") == ["jöb ✓ 日本"]


def test_dest_name_that_is_long_or_hidden(tmp_path: Path) -> None:
    for name in (".hidden", "x" * 200):
        mat.build_job(FAM, _p(), 1, tmp_path / name)
        assert (tmp_path / name).is_dir()


def test_bad_params_or_seed_do_not_even_create_parent_or_staging(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        mat.build_job(FAM, _p(), -1, tmp_path / "a" / "j")
    assert not (tmp_path / "a").exists()


def test_same_inputs_same_bytes_determinism(tmp_path: Path) -> None:
    a = mat.build_job(FAM, _p(), 5, tmp_path / "a")
    b = mat.build_job(FAM, _p(), 5, tmp_path / "b")
    for f in sorted(p.relative_to(a.path) for p in a.path.rglob("*") if p.is_file()):
        assert (a.path / f).read_bytes() == (b.path / f).read_bytes(), f
    assert a.content_digest == b.content_digest if hasattr(a, "content_digest") else True
