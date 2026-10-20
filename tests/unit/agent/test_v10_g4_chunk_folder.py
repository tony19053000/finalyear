"""V0.10 G4 fix r2 (B1): the per-chunk control folder lives outside the job's
workdir, refuses to pack a swapped folder and never deletes through a link."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from meshforge.agent.execution.chunk_folder import (
    CONTAINER_CHUNK_IN,
    CONTAINER_CHUNK_OUT,
    ChunkFolder,
    ChunkFolderSwappedError,
)
from meshforge.agent.execution.runtime import ExtraMount

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks"),
]


def _secret(tmp_path: Path) -> Path:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key").write_bytes(b"private")
    return secret


def _folder(tmp_path: Path, *, docker: bool = False) -> ChunkFolder:
    root = tmp_path / "work"
    root.mkdir()
    (root / "att_x").mkdir()  # the lane's workdir, next to the folder
    folder = ChunkFolder(root, "att_x", lines=False, docker=docker)
    folder.create()
    return folder


def test_the_folder_is_outside_the_workdir(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    assert folder.root.parent == tmp_path / "work"
    assert not folder.root.is_relative_to(tmp_path / "work" / "att_x")
    assert folder.seen_out == str(folder.out)
    docker = ChunkFolder(tmp_path / "work", "att_y", lines=True, docker=True)
    assert docker.seen_items == f"{CONTAINER_CHUNK_IN}/items.txt"
    assert docker.seen_out == CONTAINER_CHUNK_OUT
    assert docker.mounts == (
        ExtraMount(docker.inbox, CONTAINER_CHUNK_IN, read_only=True),
        ExtraMount(docker.out, CONTAINER_CHUNK_OUT, read_only=False),
    )


def test_an_honest_out_is_packed(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder.out / "a.txt").write_text("x")
    digest, size = folder.pack_out(tmp_path / "b.bundle", 1 << 20)
    assert size > 0 and digest is not None


def test_out_swapped_for_a_link_is_refused(tmp_path: Path) -> None:
    secret = _secret(tmp_path)
    folder = _folder(tmp_path)
    folder.out.rmdir()
    os.symlink(secret, folder.out)
    with pytest.raises(ChunkFolderSwappedError):
        folder.pack_out(tmp_path / "b.bundle", 1 << 20)
    assert not (tmp_path / "b.bundle").exists()


def test_out_replaced_by_another_real_folder_is_refused(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()  # made while out/ exists: surely another inode
    folder.out.rmdir()
    other.rename(folder.out)
    with pytest.raises(ChunkFolderSwappedError):
        folder.pack_out(tmp_path / "b.bundle", 1 << 20)


def test_the_whole_folder_swapped_is_refused_and_never_deleted_through(
    tmp_path: Path,
) -> None:
    secret = _secret(tmp_path)
    folder = _folder(tmp_path)
    shutil.rmtree(folder.root)
    os.symlink(secret, folder.root)
    with pytest.raises(ChunkFolderSwappedError):
        folder.pack_out(tmp_path / "b.bundle", 1 << 20)
    folder.remove()
    assert not os.path.lexists(folder.root)  # the link went ...
    assert (secret / "key").read_bytes() == b"private"  # ... its target stayed


def test_remove_and_recreate_after_a_link_inside(tmp_path: Path) -> None:
    secret = _secret(tmp_path)
    folder = _folder(tmp_path)
    os.symlink(secret, folder.out / "link")
    folder.remove()
    assert (secret / "key").exists()
    folder.create()
    assert folder.out.is_dir() and not any(folder.out.iterdir())
