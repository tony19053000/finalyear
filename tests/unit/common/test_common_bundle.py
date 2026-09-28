"""Bundles (brief D47): determinism, ignore rules, caps, and every unpack attack.

The golden-digest test runs on Linux *and* Windows in CI (unit tier): the same
tree must give the same bytes on both.
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

from meshforge.common.bundle import (
    MAX_COMPONENT_BYTES,
    MAX_PATH_BYTES,
    BundleError,
    BundleLimits,
    check_member_name,
    pack,
    pack_to_file,
    unpack,
)
from meshforge.common.errors import FailureType

WINDOWS = sys.platform.startswith("win")
posix_only = pytest.mark.skipif(WINDOWS, reason="needs POSIX symlinks/modes/FIFOs")

GOLDEN_DIGEST = "8c31d009febaa4bc32ccf6874301c7820663a370cd131450e82b170c9e0d35a9"
"""Measured on Linux (Python 3.12, zstandard 0.25.0); CI checks the same value on
windows-latest. If a zstandard upgrade changes it, that is a real compatibility
event (every stored bundle digest changes): update it deliberately, never blindly."""


def _write(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _golden_tree(root: Path) -> None:
    _write(root, "a.txt", b"hello\n")
    _write(root, "src/main.py", b"print('hi')\n")
    _write(root, "src/data/x.bin", bytes(range(256)))
    _write(root, "debug.log", b"ignored\n")
    _write(root, ".gitignore", b"*.log\n")
    _write(root, "__pycache__/m.cpython-312.pyc", b"\x00")
    (root / "empty").mkdir()


def _digest(root: Path, **kwargs: object) -> str:
    return pack(root, io.BytesIO(), **kwargs).digest.hex  # type: ignore[arg-type]


def _bytes(root: Path) -> bytes:
    out = io.BytesIO()
    pack(root, out)
    return out.getvalue()


def _archive(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return zstandard.ZstdCompressor().compress(raw.getvalue())


def _file(
    name: str, data: bytes = b"x", mode: int = 0o644
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), mode
    return info, data


def _special(
    name: str, kind: bytes, linkname: str = ""
) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info, None


def _assert_contained(sandbox: Path, dest: Path) -> None:
    """Nothing but ``dest`` in the sandbox, and ``dest`` gone or empty."""
    assert [p.name for p in sandbox.iterdir()] in ([], [dest.name])
    if dest.exists():
        assert list(dest.iterdir()) == []


# ------------------------------------------------------------- determinism


def test_same_tree_twice_gives_identical_bytes(tmp_path: Path) -> None:
    _golden_tree(tmp_path)
    assert _bytes(tmp_path) == _bytes(tmp_path)


def test_golden_digest_is_the_same_on_every_os(tmp_path: Path) -> None:
    _golden_tree(tmp_path)
    assert _digest(tmp_path) == GOLDEN_DIGEST


def test_creation_order_and_mtime_do_not_change_the_digest(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    for rel in ("b/2.txt", "a.txt", "b/1.txt", "c"):
        _write(one, rel, rel.encode())
    for rel in ("c", "b/1.txt", "a.txt", "b/2.txt"):
        _write(two, rel, rel.encode())
    os.utime(two / "a.txt", (1, 1))
    os.utime(two / "b", (1_000_000, 1_000_000))
    assert _digest(one) == _digest(two)


def test_content_changes_the_digest(tmp_path: Path) -> None:
    _write(tmp_path, "a.txt", b"1")
    before = _digest(tmp_path)
    _write(tmp_path, "a.txt", b"2")
    assert _digest(tmp_path) != before


@posix_only
def test_exec_bit_changes_the_digest_and_survives_a_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "p"
    script = _write(project, "run.sh", b"#!/bin/sh\n")
    before = _digest(project)
    script.chmod(0o755)
    assert _digest(project) != before
    script.chmod(0o775)  # group bits are normalised away
    after = _digest(project)
    script.chmod(0o700)
    assert _digest(project) == after
    archive = tmp_path / "b.tar.zst"
    pack_to_file(project, archive)
    unpack(archive, tmp_path / "out")
    assert (tmp_path / "out" / "run.sh").stat().st_mode & 0o777 == 0o755


def test_round_trip_restores_content(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _golden_tree(project)
    _write(project, "deep/" * 30 + "f.txt", b"deep")
    _write(project, "unicode-é/ñ.txt", "ü".encode())
    archive = tmp_path / "b.tar.zst"
    result = pack_to_file(project, archive)
    stats = unpack(archive, tmp_path / "out")
    assert stats.files == result.stats.files
    assert stats.total_bytes == result.stats.total_bytes
    for rel in ("a.txt", "src/data/x.bin", "deep/" * 30 + "f.txt", "unicode-é/ñ.txt"):
        assert (tmp_path / "out" / rel).read_bytes() == (project / rel).read_bytes()
    assert (tmp_path / "out" / "empty").is_dir()
    assert not (tmp_path / "out" / "debug.log").exists()


def test_pack_to_file_inside_the_tree_excludes_itself(tmp_path: Path) -> None:
    _golden_tree(tmp_path)
    first = pack_to_file(tmp_path, tmp_path / "bundle.tar.zst")
    second = pack_to_file(tmp_path, tmp_path / "bundle.tar.zst")
    assert first.digest == second.digest
    plain = pack(tmp_path, io.BytesIO(), exclude=["bundle.tar.zst"])
    assert plain.digest == first.digest
    assert [p.name for p in tmp_path.glob(".bundle*")] == []


# ------------------------------------------------------------- ignore rules


def _packed_names(root: Path) -> set[str]:
    out = tmp = root.parent / f"{root.name}-out"
    unpack(io.BytesIO(_bytes(root)), tmp)
    return {p.relative_to(out).as_posix() for p in out.rglob("*")}


def test_builtin_ignores(tmp_path: Path) -> None:
    root = tmp_path / "p"
    for rel in (
        ".git/config",
        "__pycache__/x.pyc",
        ".venv/bin/python",
        "node_modules/x/i.js",
        "a.pyc",
        "sub/b.pyc",
        "keep.py",
    ):
        _write(root, rel)
    assert _packed_names(root) == {"keep.py", "sub"}


def test_gitignore_negation_nested_and_computeignore(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root, ".gitignore", b"*.log\n!keep.log\ndata/\n# comment\n\n")
    _write(root, ".computeignore", b"!data/\nsecret.txt\n")
    for rel in (
        "a.log",
        "keep.log",
        "data/d.csv",
        "secret.txt",
        "sub/secret.txt",
        "sub/x.log",
        "sub/.gitignore",
        "sub/anch",
        "sub/deep/anch",
        "sub/deep/tmp.bin",
    ):
        _write(root, rel)
    _write(root, "sub/.gitignore", b"/anch\ntmp.bin\n")
    names = _packed_names(root)
    assert "a.log" not in names and "sub/x.log" not in names
    assert "keep.log" in names
    assert "data/d.csv" in names  # .computeignore re-includes what .gitignore excluded
    assert "secret.txt" not in names and "sub/secret.txt" not in names
    assert "sub/anch" not in names  # anchored to sub/
    assert "sub/deep/anch" in names
    assert "sub/deep/tmp.bin" not in names  # unanchored: any depth below sub/


def test_file_under_an_ignored_folder_cannot_be_reincluded(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root, ".gitignore", b"build/\n!build/keep.txt\n")
    _write(root, "build/keep.txt")
    assert "build/keep.txt" not in _packed_names(root)


def test_folder_names_with_glob_characters_scope_their_rules(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root, "[x]/.gitignore", b"a.txt\n")
    _write(root, "[x]/a.txt")
    _write(root, "x/a.txt")
    names = _packed_names(root)
    assert "[x]/a.txt" not in names
    assert "x/a.txt" in names


def test_oversized_ignore_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, ".gitignore", b"#" * (1024 * 1024 + 1))
    with pytest.raises(BundleError, match="larger than"):
        pack(tmp_path, io.BytesIO())


# --------------------------------------------------------------------- caps


def test_file_count_cap_max_ok_and_max_plus_one_refused(tmp_path: Path) -> None:
    for i in range(5):
        _write(tmp_path, f"f{i}")
    pack(tmp_path, io.BytesIO(), limits=BundleLimits(max_files=5))
    with pytest.raises(BundleError, match="more than 4"):
        pack(tmp_path, io.BytesIO(), limits=BundleLimits(max_files=4))


def test_total_size_cap_max_ok_and_max_plus_one_refused(tmp_path: Path) -> None:
    _write(tmp_path, "a", b"x" * 100)
    pack(tmp_path, io.BytesIO(), limits=BundleLimits(max_total_bytes=100))
    with pytest.raises(BundleError, match="larger than 99"):
        pack(tmp_path, io.BytesIO(), limits=BundleLimits(max_total_bytes=99))


def test_unpack_caps_max_ok_and_max_plus_one_refused(tmp_path: Path) -> None:
    data = _archive(_file("a", b"x" * 100), _file("b", b"y"))
    unpack(io.BytesIO(data), tmp_path / "ok", limits=BundleLimits(max_total_bytes=101))
    with pytest.raises(BundleError, match="larger than 100"):
        unpack(
            io.BytesIO(data), tmp_path / "big", limits=BundleLimits(max_total_bytes=100)
        )
    unpack(io.BytesIO(data), tmp_path / "ok2", limits=BundleLimits(max_files=2))
    with pytest.raises(BundleError, match="more than 1"):
        unpack(io.BytesIO(data), tmp_path / "many", limits=BundleLimits(max_files=1))
    assert not (tmp_path / "big").exists() and not (tmp_path / "many").exists()


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("a" * MAX_COMPONENT_BYTES, True),
        ("a" * (MAX_COMPONENT_BYTES + 1), False),
        ("/".join(["a" * 255] * 16) + "/" + "b" * 1, None),
    ],
)
def test_member_name_length_bounds(name: str, ok: bool | None) -> None:
    if ok is None:  # construct exactly MAX_PATH_BYTES and MAX_PATH_BYTES + 1
        base = "/".join(["a" * 200] * 20)  # 4019 bytes
        at_max = base + "/" + "b" * (MAX_PATH_BYTES - len(base) - 1)
        assert len(at_max) == MAX_PATH_BYTES
        check_member_name(at_max)
        with pytest.raises(BundleError, match="longer than"):
            check_member_name(at_max + "c")
        return
    if ok:
        check_member_name(name)
    else:
        with pytest.raises(BundleError):
            check_member_name(name)


# ------------------------------------------------------------ pack refusals


@posix_only
@pytest.mark.parametrize("target", ["../outside.txt", "/etc/passwd", "a/../../x"])
def test_pack_refuses_symlinks_leaving_the_tree(tmp_path: Path, target: str) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    os.symlink(target, root / "link")
    with pytest.raises(BundleError, match="symlink") as info:
        pack(root, io.BytesIO())
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert info.value.details == {"path": "link"}


@posix_only
def test_pack_refuses_a_symlink_chain_that_resolves_outside(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / "a" / "b").mkdir(parents=True)
    os.symlink("../..", root / "a" / "b" / "up")  # -> root (inside)
    os.symlink("a/b/up/../../..", root / "l1")  # lexically ".", really above root
    with pytest.raises(BundleError, match="outside"):
        pack(root, io.BytesIO())


@posix_only
def test_pack_keeps_inside_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root, "sub/real.txt", b"r")
    os.symlink("sub/real.txt", root / "link")
    os.symlink("..", root / "sub" / "up")
    result = pack(root, io.BytesIO())
    assert result.stats.symlinks == 2


@posix_only
def test_pack_refuses_a_fifo_and_a_non_utf8_name(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(BundleError, match="FIFO"):
        pack(root, io.BytesIO())
    (root / "pipe").unlink()
    if sys.platform == "darwin":
        pytest.skip("macOS refuses non-UTF-8 names itself")
    (Path(os.fsdecode(bytes(root))) / os.fsdecode(b"bad\xff")).write_bytes(b"x")
    with pytest.raises(BundleError, match="UTF-8"):
        pack(root, io.BytesIO())


@posix_only
def test_pack_packs_a_hardlinked_file_as_ordinary_content(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root, "a", b"same")
    os.link(root / "a", root / "b")
    unpack(io.BytesIO(_bytes(root)), tmp_path / "out")
    assert (tmp_path / "out" / "b").read_bytes() == b"same"
    assert (tmp_path / "out" / "b").stat().st_nlink == 1


def test_pack_refuses_a_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(BundleError):
        pack(tmp_path / "nope", io.BytesIO())


# ----------------------------------------------------------- unpack attacks


@pytest.mark.parametrize(
    "name",
    [
        "/abs/evil.txt",
        "../evil.txt",
        "a/../../evil.txt",
        "./a",
        "a//b",
        "C:/evil",
        "a\\..\\evil",
    ],
)
def test_unpack_refuses_hostile_member_names(tmp_path: Path, name: str) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    dest = sandbox / "dest"
    data = _archive(_file("ok.txt"), _file(name, b"evil"))
    with pytest.raises(BundleError) as info:
        unpack(io.BytesIO(data), dest)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    _assert_contained(sandbox, dest)
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize("link", ["../../outside", "/etc/passwd", "a/../../x", "C:/x"])
def test_unpack_refuses_escaping_symlinks(tmp_path: Path, link: str) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    dest = sandbox / "dest"
    data = _archive(_special("l", tarfile.SYMTYPE, link))
    with pytest.raises(BundleError, match="symlink"):
        unpack(io.BytesIO(data), dest)
    _assert_contained(sandbox, dest)


def test_unpack_refuses_writing_through_a_symlink(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    (sandbox / "victim").mkdir(parents=True)
    dest = sandbox / "dest"
    data = _archive(
        _special("sub", tarfile.SYMTYPE, "."), _file("sub/evil.txt", b"evil")
    )
    with pytest.raises(BundleError):
        unpack(io.BytesIO(data), dest)
    assert not (sandbox / "evil.txt").exists()
    assert list((sandbox / "victim").iterdir()) == []


@posix_only
def test_unpack_refuses_a_symlink_chain_resolving_outside(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    dest = sandbox / "dest"
    dirs = []
    for name in ("a", "a/b"):
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        dirs.append((info, None))
    data = _archive(
        *dirs,
        _special("a/b/up", tarfile.SYMTYPE, "../.."),
        _special("l1", tarfile.SYMTYPE, "a/b/up/../../.."),
    )
    with pytest.raises(BundleError, match="outside"):
        unpack(io.BytesIO(data), dest)
    _assert_contained(sandbox, dest)


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        (tarfile.LNKTYPE, "hard links"),
        (tarfile.CHRTYPE, "device"),
        (tarfile.BLKTYPE, "device"),
        (tarfile.FIFOTYPE, "FIFO"),
    ],
)
def test_unpack_refuses_hardlinks_devices_and_fifos(
    tmp_path: Path, kind: bytes, fragment: str
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    dest = sandbox / "dest"
    data = _archive(_file("a.txt"), _special("x", kind, "a.txt"))
    with pytest.raises(BundleError, match=fragment):
        unpack(io.BytesIO(data), dest)
    _assert_contained(sandbox, dest)


def test_unpack_refuses_duplicates_and_file_over_file(tmp_path: Path) -> None:
    data = _archive(_file("a.txt", b"1"), _file("a.txt", b"2"))
    with pytest.raises(BundleError, match="duplicate"):
        unpack(io.BytesIO(data), tmp_path / "d1")
    data = _archive(_file("a", b"1"), _file("a/b", b"2"))
    with pytest.raises(BundleError, match="under a symlink or a file"):
        unpack(io.BytesIO(data), tmp_path / "d2")


def test_decompression_bomb_is_refused_before_it_reaches_the_disk(
    tmp_path: Path,
) -> None:
    zeros = b"\x00" * (64 * 1024 * 1024)
    data = _archive(_file("bomb.bin", zeros))
    assert len(data) < 64 * 1024  # a real bomb: tiny on the wire
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    dest = sandbox / "dest"
    with pytest.raises(BundleError, match="larger than"):
        unpack(io.BytesIO(data), dest, limits=BundleLimits(max_total_bytes=1024 * 1024))
    _assert_contained(sandbox, dest)


def test_lying_size_header_is_refused_by_declared_size(tmp_path: Path) -> None:
    info = tarfile.TarInfo("huge")
    info.size = 8 * 1024**3 - 1  # the largest a ustar header can declare
    header = info.tobuf(tarfile.USTAR_FORMAT)
    data = zstandard.ZstdCompressor().compress(header + b"\x00" * 1024)
    with pytest.raises(BundleError, match="larger than"):
        unpack(io.BytesIO(data), tmp_path / "d")


def test_decompressed_stream_bound(tmp_path: Path) -> None:
    data = _archive(*[_file(f"f{i}", b"x" * 100) for i in range(50)])
    unpack(io.BytesIO(data), tmp_path / "ok")
    with pytest.raises(BundleError, match="decompression bomb"):
        unpack(
            io.BytesIO(data),
            tmp_path / "bounded",
            limits=BundleLimits(max_decompressed_bytes=8 * 1024),
        )
    assert not (tmp_path / "bounded").exists()


def test_oversized_pax_header_is_refused_before_it_is_read(tmp_path: Path) -> None:
    info = tarfile.TarInfo("././@PaxHeader")
    info.type = tarfile.XHDTYPE
    info.size = 1024**3
    data = zstandard.ZstdCompressor().compress(info.tobuf(tarfile.USTAR_FORMAT))
    with pytest.raises(BundleError, match="metadata header"):
        unpack(io.BytesIO(data), tmp_path / "d")


@pytest.mark.parametrize(
    "blob",
    [b"not zstd at all", zstandard.ZstdCompressor().compress(b"not a tar" * 100), b""],
)
def test_corrupt_archives_are_typed_errors(tmp_path: Path, blob: bytes) -> None:
    with pytest.raises(BundleError):
        unpack(io.BytesIO(blob), tmp_path / "d")
    assert not (tmp_path / "d").exists()


def test_truncated_archive_leaves_nothing(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _write(project, "a.bin", os.urandom(200_000))
    data = _bytes(project)
    with pytest.raises(BundleError):
        unpack(io.BytesIO(data[: len(data) // 2]), tmp_path / "d")
    assert not (tmp_path / "d").exists()


def test_existing_non_empty_destination_is_refused(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "keep").write_text("mine")
    with pytest.raises(BundleError, match="new or empty"):
        unpack(io.BytesIO(_archive(_file("a"))), tmp_path / "d")
    assert (tmp_path / "d" / "keep").read_text() == "mine"


def test_failed_unpack_into_an_existing_empty_folder_empties_it(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(BundleError):
        unpack(io.BytesIO(_archive(_file("a"), _file("../x"))), tmp_path / "d")
    assert (tmp_path / "d").is_dir()
    assert list((tmp_path / "d").iterdir()) == []


@pytest.mark.parametrize(
    "name", ["ab:c", "CON", "nul.txt", "trailing.", "trailing ", "q?", "tab\tname"]
)
def test_windows_only_name_rules(name: str) -> None:
    check_member_name(name, windows=False)
    with pytest.raises(BundleError, match="Windows"):
        check_member_name(name, windows=True)


def test_setuid_and_other_mode_bits_are_never_restored(tmp_path: Path) -> None:
    data = _archive(_file("a", b"x", mode=0o6777))
    unpack(io.BytesIO(data), tmp_path / "d")
    if not WINDOWS:
        assert (tmp_path / "d" / "a").stat().st_mode & 0o7777 == 0o755


def test_nul_and_empty_member_names_are_refused() -> None:
    for name in ("nul\x00byte", ""):
        with pytest.raises(BundleError, match="NUL"):
            check_member_name(name)
