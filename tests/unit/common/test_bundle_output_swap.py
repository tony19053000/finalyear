"""V0.4 G5 fix round (reviewer N4): an output swapped after it was listed is refused.

``pack_files`` lists each output with ``lstat`` and opens it later to pack it.
Between the two a workload (or anything else writing the workdir) could swap the
name for a symlink, another file, a FIFO, or swap a parent folder for a link.
The open descriptor is now checked (``O_NOFOLLOW`` + ``fstat``: regular file,
same ``(st_dev, st_ino)`` as listed), so the swap is a :class:`BundleError` and
no byte from outside the workdir is ever packed.

The swap is made deterministic by running it between the listing and the pack
(``_pack_entries`` wrapped).
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshforge.common import bundle
from meshforge.common.bundle import BundleError, pack_files

pytestmark = pytest.mark.unit

SECRET = b"outside the workdir - must never be packed\n"
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks/FIFOs")


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "out").mkdir(parents=True)
    # same size as SECRET, so no size check can mask a missing identity check
    (root / "out" / "result.txt").write_bytes(b"r" * len(SECRET))
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "result.txt").write_bytes(SECRET)
    return root


def _swap_before_pack(
    monkeypatch: pytest.MonkeyPatch, swap: Callable[[], None]
) -> None:
    real = bundle._pack_entries

    def wrapped(state: Any, out: Any) -> Any:
        swap()
        return real(state, out)

    monkeypatch.setattr(bundle, "_pack_entries", wrapped)


def _pack(work: Path) -> bytes:
    out = io.BytesIO()
    pack_files(work, ["out/result.txt"], out)
    return out.getvalue()


def test_unswapped_output_packs(work: Path) -> None:
    assert _pack(work)


@posix_only
@pytest.mark.parametrize("nofollow", [True, False], ids=["posix", "no-O_NOFOLLOW"])
def test_a_file_swapped_for_a_symlink_is_refused(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nofollow: bool
) -> None:
    if not nofollow:  # what Windows has: the fstat identity check must catch it
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    target = work / "out" / "result.txt"

    def swap() -> None:
        target.unlink()
        target.symlink_to(tmp_path / "outside" / "result.txt")

    _swap_before_pack(monkeypatch, swap)
    with pytest.raises(BundleError, match="cannot read" if nofollow else "replaced"):
        _pack(work)


def test_a_file_swapped_for_another_file_is_refused(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = work / "out" / "result.txt"
    planted = work / "planted"
    planted.write_bytes(SECRET)

    _swap_before_pack(monkeypatch, lambda: os.replace(planted, target))
    with pytest.raises(BundleError, match="replaced"):
        _pack(work)


@posix_only
def test_a_parent_folder_swapped_for_a_symlink_is_refused(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # parents are followed anyway

    def swap() -> None:
        (work / "out").rename(work / "moved")
        (work / "out").symlink_to(tmp_path / "outside")

    _swap_before_pack(monkeypatch, swap)
    with pytest.raises(BundleError, match="replaced"):
        _pack(work)


@posix_only
@pytest.mark.timeout(10)
def test_a_file_swapped_for_a_fifo_is_refused_without_blocking(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = work / "out" / "result.txt"

    def swap() -> None:
        target.unlink()
        os.mkfifo(target)

    _swap_before_pack(monkeypatch, swap)
    with pytest.raises(BundleError, match="replaced"):
        _pack(work)
