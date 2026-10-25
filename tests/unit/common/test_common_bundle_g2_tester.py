"""Tester round 1 (V0.4 G2): bundle edge cases not covered by the implementer's suite.

Symlink cycles at pack time (a self-referencing chain fully inside the tree, never
escaping it -- what does :func:`pack` do: hang, crash, or a typed error?), a
truncated-but-otherwise-valid zstd frame, and same-name-different-case files on a
case-sensitive filesystem (documents current behaviour; V0.4's target laptops are
Linux + Windows, and Windows' default filesystem is case-insensitive -- a real risk
this suite cannot reproduce on this machine, flagged as a defect/follow-up below).
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

from meshforge.common.bundle import BundleError, pack, pack_to_file, unpack

WINDOWS = sys.platform.startswith("win")
posix_only = pytest.mark.skipif(WINDOWS, reason="needs POSIX symlinks")


def _write(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _archive(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return zstandard.ZstdCompressor().compress(raw.getvalue())


def _file(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


# --------------------------------------------------------- symlink cycles (pack)


@posix_only
def test_pack_a_symlink_cycle_fully_inside_the_tree_does_not_hang_or_crash(
    tmp_path: Path,
) -> None:
    """a -> b -> a, neither ever resolving outside the project: must terminate
    (BundleError or a clean pack), never hang, never an unhandled exception."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "a").symlink_to("b")
    (project / "b").symlink_to("a")
    out = io.BytesIO()
    try:
        pack(project, out)
    except BundleError:
        pass  # acceptable: the cycle is refused
    else:
        pass  # acceptable: packed as two opaque symlink entries (never followed)


@posix_only
def test_pack_a_three_hop_symlink_cycle_terminates(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    (project / "a").symlink_to("b")
    (project / "b").symlink_to("c")
    (project / "c").symlink_to("a")
    out = io.BytesIO()
    try:
        pack(project, out)
    except BundleError:
        pass  # acceptable: the cycle is refused with our typed error
    except RecursionError:
        pytest.fail(
            "a 3-hop symlink cycle inside the tree crashed pack() with an "
            "internal RecursionError instead of a typed BundleError"
        )


# ---------------------------------------------------- truncated-but-valid zstd


def test_truncated_valid_zstd_frame_is_a_typed_error_not_a_crash(
    tmp_path: Path,
) -> None:
    """A frame with a correct magic/header but whose body is cut mid-stream (as
    opposed to cutting the raw tar bytes before compression, which the
    implementer's suite already covers) must still be a BundleError."""
    project = tmp_path / "p"
    _write(project, "a.bin", os.urandom(500_000))  # big enough to span frames
    full = io.BytesIO()
    pack(project, full)
    data = full.getvalue()
    truncated = data[: len(data) // 3]  # cut deep inside the compressed body
    with pytest.raises(BundleError):
        unpack(io.BytesIO(truncated), tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


# --------------------------------------------- case-insensitive-filesystem risk


@pytest.mark.skipif(
    sys.platform in ("win32", "darwin"),
    reason="needs a case-sensitive file system (NTFS/APFS fold case)",
)
def test_two_files_differing_only_by_case_pack_and_unpack_as_distinct_files(
    tmp_path: Path,
) -> None:
    """On THIS (case-sensitive) filesystem, A.py and a.py are unrelated files and
    round-trip cleanly. This is a defect flag, not a pass/fail assertion: V0.4's
    target fleet includes Windows laptops, whose default filesystem (NTFS) is
    case-insensitive. An archive packed on Linux with both `A.py` and `a.py`
    would silently clobber one on unpack there (the second `_write_file` call
    would collide with the first at the OS level, not at our dedupe-by-exact-name
    check, since our `state.files` set is case-sensitive). This suite cannot
    reproduce that failure mode on this machine; it documents the current,
    unverified-on-Windows behaviour so a human running the 0.4.15 lab checklist
    on a Windows laptop can check it for real.
    """
    project = tmp_path / "p"
    _write(project, "A.py", b"upper\n")
    _write(project, "a.py", b"lower\n")
    packed = tmp_path / "bundle.tar.zst"
    pack_to_file(project, packed)
    dest = tmp_path / "out"
    unpack(packed, dest)
    assert (dest / "A.py").read_bytes() == b"upper\n"
    assert (dest / "a.py").read_bytes() == b"lower\n"


# ------------------------------------------------------- unpack into non-empty


def test_unpack_into_a_dest_containing_only_hidden_dotfiles_is_still_refused(
    tmp_path: Path,
) -> None:
    """`_prepare_dest` treats "empty" as *zero entries*, including dotfiles --
    a stricter reading of "empty" than a naive `not any(p for p in iterdir() if
    not p.name.startswith('.'))` would give. Confirms the guard is not fooled by
    a leftover dotfile."""
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".leftover").write_bytes(b"x")
    project = tmp_path / "p"
    _write(project, "a.txt", b"hi\n")
    packed = tmp_path / "b.tar.zst"
    pack_to_file(project, packed)
    with pytest.raises(BundleError, match="new or empty"):
        unpack(packed, dest)
    assert (dest / ".leftover").exists()  # refused before anything changed
