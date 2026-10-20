"""V0.10 G5 (D283 pin 4, the G4 B1 rules): the combine's control folder lives
outside the workdir, its parts are read-only and ordered by name, a swapped
folder is never packed, removal never follows a link; placeholders and env."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from meshforge.agent.execution.combine_folder import (
    CONTAINER_COMBINE_OUT,
    CONTAINER_PARTS,
    CombineFolder,
    CombineFolderSwappedError,
    combine_argv,
    combine_env,
    part_width,
)
from meshforge.agent.execution.outputs import OutputError
from meshforge.agent.execution.runtime import ExtraMount
from meshforge.common.bundle import unpack

pytestmark = [pytest.mark.unit]

POSIX = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes/links")


def _folder(tmp_path: Path, *, chunks: int = 3, docker: bool = False) -> CombineFolder:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    (root / "att_x").mkdir(exist_ok=True)  # the attempt's workdir, next to it
    folder = CombineFolder(root, "att_x", chunks=chunks, docker=docker)
    folder.create()
    return folder


@pytest.mark.parametrize(
    ("chunks", "width"), [(1, 5), (99_999, 5), (100_000, 6), (10_000, 5)]
)
def test_part_width(chunks: int, width: int) -> None:
    assert part_width(chunks) == width


def test_the_folder_is_next_to_the_workdir_never_inside(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    assert folder.root == tmp_path / "work" / "att_x.combine"
    assert folder.parts.is_dir() and folder.out.is_dir()
    assert folder.part(0).name == "00000"
    assert folder.part(2).name == "00002"
    with pytest.raises(ValueError):
        folder.part(3)
    assert folder.seen_parts == str(folder.parts)
    assert folder.mounts == ()


def test_docker_binds_parts_read_only_and_out_read_write(tmp_path: Path) -> None:
    folder = _folder(tmp_path, docker=True)
    assert folder.seen_parts == CONTAINER_PARTS
    assert folder.seen_out == CONTAINER_COMBINE_OUT
    assert folder.mounts == (
        ExtraMount(folder.parts, CONTAINER_PARTS, read_only=True),
        ExtraMount(folder.out, CONTAINER_COMBINE_OUT, read_only=False),
    )


@POSIX
def test_sealed_parts_are_read_only_and_resumable(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    part = folder.part(1)
    part.mkdir()
    (part / "a.txt").write_bytes(b"x")
    assert not folder.has_part(1)  # unpacked but not sealed: not counted
    folder.seal_part(1)
    assert folder.has_part(1)
    assert not os.stat(part / "a.txt").st_mode & stat.S_IWUSR
    folder.drop_part(1)  # a half-made part goes (read-only or not)
    assert not part.exists()
    folder.seal()
    assert not os.stat(folder.parts).st_mode & stat.S_IWUSR
    folder.remove()  # read-only parts are removed too
    assert not folder.root.exists()


def test_pack_out_is_deterministic_and_unpacks_to_out(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder.out / "r.csv").write_bytes(b"a\n1\n")
    (folder.out / "sub").mkdir()
    (folder.out / "sub" / "x.json").write_bytes(b"[]\n")
    first = folder.pack_out(tmp_path / "one.bundle", 1 << 20)
    second = folder.pack_out(tmp_path / "two.bundle", 1 << 20)
    assert first == second
    unpack(tmp_path / "one.bundle", tmp_path / "check")
    assert (tmp_path / "check" / "sub" / "x.json").read_bytes() == b"[]\n"


def test_an_empty_out_is_an_empty_bundle(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    digest, size = folder.pack_out(tmp_path / "e.bundle", 1 << 20)
    assert size > 0 and str(digest).startswith("sha256:")


def test_pack_out_over_the_limit_is_output_limit(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder.out / "big").write_bytes(os.urandom(64 * 1024))
    with pytest.raises(OutputError) as caught:
        folder.pack_out(tmp_path / "b.bundle", 1024)
    assert caught.value.message.startswith("output_limit")
    assert not (tmp_path / "b.bundle").exists()


@POSIX
def test_a_swapped_out_folder_is_never_packed(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key").write_bytes(b"private")
    folder = _folder(tmp_path)
    os.rmdir(folder.out)
    folder.out.symlink_to(secret, target_is_directory=True)
    with pytest.raises(CombineFolderSwappedError):
        folder.pack_out(tmp_path / "s.bundle", 1 << 20)
    assert not (tmp_path / "s.bundle").exists()


@POSIX
def test_removal_never_follows_a_link(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key").write_bytes(b"private")
    folder = _folder(tmp_path)
    os.rmdir(folder.out)
    folder.out.symlink_to(secret, target_is_directory=True)
    folder.remove()
    assert (secret / "key").read_bytes() == b"private"
    assert not folder.root.exists()
    # the whole control folder swapped for a link: only the link goes
    root = folder.root
    root.symlink_to(secret, target_is_directory=True)
    folder.remove()
    assert not os.path.lexists(root)
    assert (secret / "key").read_bytes() == b"private"


def test_create_replaces_what_was_left(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder.out / "old").write_bytes(b"stale")
    folder.create()
    assert list(folder.out.iterdir()) == []


def test_placeholders_and_env() -> None:
    argv = combine_argv(
        ("python", "c.py", "--in={parts}", "{out}", "{items_file}", "{parts}{out}"),
        parts="/p/{out}",
        out="/o",
    )
    # replaced once, left to right, never rescanned; other names stay literal
    assert argv == (
        "python",
        "c.py",
        "--in=/p/{out}",
        "/o",
        "{items_file}",
        "/p/{out}/o",
    )
    env = combine_env({"A": "1"}, parts="/p", out="/o")
    assert env == {
        "A": "1",
        "MESHFORGE_COMBINE_PARTS": "/p",
        "MESHFORGE_COMBINE_OUT": "/o",
    }
