"""D62-4: unpacking onto a case-insensitive filesystem (Windows NTFS, macOS APFS).

A bundle built on Linux may hold ``a.txt`` *and* ``A.txt``, or a symlink ``Lib``
next to a folder ``lib/``. On a case-insensitive filesystem these are the same
path: the second member must be **refused** (typed ``invalid_input``, destination
left empty) - never silently overwrite the first, and never be written *through*
a symlink that only matches case-insensitively.

These tests need a case-insensitive directory. Linux ``tmp`` is case-sensitive and
cannot emulate one without root (vfat mount / ``chattr +F`` on a casefold ext4),
so they SKIP there with that reason and run on Windows/macOS CI and the 0.4.15
lab laptops.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
import zstandard

from meshforge.common.bundle import BundleError, unpack
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


@pytest.fixture
def ci_dir(tmp_path: Path) -> Path:
    if not _case_insensitive(tmp_path):
        pytest.skip(
            "needs a case-insensitive filesystem (Windows/macOS); this one is "
            "case-sensitive and cannot emulate one without root"
        )
    return tmp_path


def _archive(*members: tuple[str, bytes | None, str | None]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                tar.addfile(info)
            elif data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(raw.getvalue())


def test_two_files_differing_only_in_case_are_refused(ci_dir: Path) -> None:
    dest = ci_dir / "out"
    bundle = _archive(("a.txt", b"first", None), ("A.txt", b"second", None))
    with pytest.raises(BundleError) as info:
        unpack(io.BytesIO(bundle), dest)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert not dest.exists() or not any(dest.iterdir())


def test_a_folder_under_a_case_twin_symlink_is_refused(ci_dir: Path) -> None:
    dest = ci_dir / "out"
    bundle = _archive(("Lib", None, "."), ("lib/evil.py", b"x", None))
    with pytest.raises(BundleError):
        unpack(io.BytesIO(bundle), dest)
    assert not dest.exists() or not any(dest.iterdir())


def test_the_probe_itself_is_honest(tmp_path: Path) -> None:
    """The skip decision rests on the probe; pin that it reads the filesystem."""
    (tmp_path / "Upper").write_text("x")
    assert _case_insensitive(tmp_path) is (tmp_path / "upper").exists()
