"""Copy a tree from the agent's cache into a workdir (V0.5 G1, D84).

**Copy, never hard-link.** A job writes into its workdir; a hard link would let
it change the cached blob every later job trusts. A copy costs little next to the
transfer. Where the file system can **reflink** (Linux ``FICLONE``: btrfs, XFS)
the copy shares extents without sharing writes; otherwise bytes are streamed.

**Hash while copying.** Every file's bytes are hashed on their way into the
workdir (after a reflink, the clone is read back and hashed). A mismatch means the
cached blob is corrupt: the copy is deleted, the blob is evicted from the cache
and :class:`CorruptBlobError` names it, so the caller fetches it again - corrupt
bytes are never left in a workdir (criterion 4, checklist 15).

**Hostile-tree safe.** The tree was validated by
:func:`~meshforge.common.tree.parse_tree` (paths, order, nothing under a file or
link). Files are created with ``O_EXCL`` (and ``O_NOFOLLOW`` on POSIX) in a
destination that must be new or empty; **symlinks are created last**, after every
file and folder, so no write can pass through one; each link must then resolve
inside the destination. Before writing, the free disk must cover the tree plus
``free_disk_floor_bytes`` (``disk_full`` otherwise). Any failure empties the
destination (or removes it, if this call created it).

Blocking: run it in a thread.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import os
import shutil
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.cas import CasNotFoundError, CasStore
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.tree import Tree, TreeEntry

__all__ = [
    "CorruptBlobError",
    "MaterialiseError",
    "MaterialiseStats",
    "materialise",
    "materialise_subset",
    "remove_subset",
]

_COPY: Final = 1024 * 1024
_FICLONE: Final = 0x40049409
"""``FICLONE`` ioctl number (linux/fs.h)."""


class MaterialiseError(MeshForgeError):
    default_failure_type = FailureType.INTERNAL_ERROR


class CorruptBlobError(MaterialiseError):
    """A cached blob's bytes do not match its name (it was evicted)."""

    default_failure_type = FailureType.INTEGRITY_ERROR

    def __init__(self, digest: Digest) -> None:
        super().__init__(
            f"cached blob {digest} is corrupt (evicted; fetch it again)",
            details={"digest": str(digest)},
        )
        self.digest = digest


@dataclass(slots=True)
class MaterialiseStats:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    bytes: int = 0
    reflinked: int = 0


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _try_reflink(src_fd: int, dst_fd: int) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        import fcntl

        fcntl.ioctl(dst_fd, _FICLONE, src_fd)
    except OSError:
        return False
    return True


def _copy_file(
    store: CasStore, entry: TreeEntry, path: Path, stats: MaterialiseStats
) -> None:
    mode = 0o755 if entry.executable else 0o644
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    digest = entry.digest
    try:
        src = store.open_read(digest)
    except CasNotFoundError:
        raise MaterialiseError(
            f"blob {digest} is not in the cache", failure_type=FailureType.NOT_FOUND
        ) from None
    hasher = hashlib.sha256()
    copied = 0
    with src:
        fd = os.open(path, flags, mode)
        with os.fdopen(fd, "wb+") as out:
            if hasattr(os, "fchmod"):
                os.fchmod(out.fileno(), mode)  # umask-independent
            if _try_reflink(src.fileno(), out.fileno()):
                stats.reflinked += 1
                out.seek(0)
                while data := out.read(_COPY):
                    hasher.update(data)
                    copied += len(data)
            else:
                while data := src.read(_COPY):
                    hasher.update(data)
                    out.write(data)
                    copied += len(data)
    if copied != entry.size or not hmac.compare_digest(
        hasher.hexdigest(), entry.sha256
    ):
        with contextlib.suppress(OSError):
            path.unlink()
        store.evict(digest)
        raise CorruptBlobError(digest)
    stats.files += 1
    stats.bytes += copied


def _prepare_dest(dest: Path) -> bool:
    try:
        dest.mkdir(parents=True)
    except FileExistsError:
        if dest.is_symlink() or not dest.is_dir() or any(dest.iterdir()):
            raise MaterialiseError(
                "the workdir must be a new or empty folder",
                failure_type=FailureType.INVALID_INPUT,
            ) from None
        return False
    return True


def _clean(dest: Path, created: bool) -> None:
    if created:
        shutil.rmtree(dest, ignore_errors=True)
        return
    for child in list(dest.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                child.unlink()


def _place(dest: Path, rel: str) -> Path:
    parts = rel.split("/")
    parent = dest.joinpath(*parts[:-1])
    parent.mkdir(parents=True, exist_ok=True)
    real_dest = os.path.realpath(dest)
    if os.path.commonpath([os.path.realpath(parent), real_dest]) != real_dest:
        raise MaterialiseError(
            "a folder resolves outside the workdir",
            failure_type=FailureType.INVALID_INPUT,
            details={"path": rel[:200]},
        )
    return dest.joinpath(*parts)


def _check_disk(
    tree: Tree, dest: Path, floor: int, disk_free: Callable[[Path], int] | None
) -> None:
    needed = tree.total_bytes
    free = (disk_free or _disk_free)(dest)
    if free - needed < floor:
        raise MaterialiseError(
            f"not enough disk for the job's files ({needed} bytes, keeping "
            f"{floor} free)",
            failure_type=FailureType.DISK_FULL,
        )


def _link(entry: TreeEntry, dest: Path, real_dest: str) -> None:
    path = _place(dest, entry.path)
    try:
        os.symlink(entry.link, path)
    except OSError as exc:
        raise MaterialiseError(
            f"cannot create a symlink here ({exc.strerror or exc}; Windows "
            "needs Developer Mode)",
            failure_type=FailureType.ENV_ERROR,
            details={"path": entry.path[:200]},
        ) from None
    if os.path.commonpath([os.path.realpath(path), real_dest]) != real_dest:
        raise MaterialiseError(
            "a symlink resolves outside the workdir",
            failure_type=FailureType.INVALID_INPUT,
            details={"path": entry.path[:200]},
        )


def _fill(store: CasStore, tree: Tree, dest: Path, stats: MaterialiseStats) -> None:
    """Folders and files first, symlinks last.

    Why this is safe against a hostile tree: while files are written no symlink
    exists in ``dest`` yet, so ``_place``'s ``mkdir(parents=True)`` and the
    ``O_EXCL | O_NOFOLLOW`` create can only ever touch real folders inside
    ``dest`` (``parse_tree`` already refused ``..``, absolute paths and entries
    under a file or link; a case-insensitive collision fails ``O_EXCL``). Each
    link is checked to resolve inside ``dest`` right after it is made, and any
    failure empties ``dest`` (:func:`materialise`), so a refused tree never
    leaves a link or a partial file behind for the job to use.
    """
    links: list[TreeEntry] = []
    for entry in tree.entries:
        if entry.kind == "symlink":
            links.append(entry)
        elif entry.kind == "dir":
            _place(dest, entry.path).mkdir(mode=0o755)
            stats.dirs += 1
        else:
            _copy_file(store, entry, _place(dest, entry.path), stats)
    real_dest = os.path.realpath(dest)
    for entry in links:  # last: no write can pass through a link
        _link(entry, dest, real_dest)
        stats.symlinks += 1


def materialise(
    store: CasStore,
    tree: Tree,
    dest: Path,
    *,
    free_disk_floor_bytes: int = 0,
    disk_free: Callable[[Path], int] | None = None,
) -> MaterialiseStats:
    """Copy ``tree`` from ``store`` into ``dest`` (see module docstring)."""
    dest = Path(dest)
    created = _prepare_dest(dest)
    stats = MaterialiseStats()
    try:
        _check_disk(tree, dest, free_disk_floor_bytes, disk_free)
        _fill(store, tree, dest, stats)
    except MaterialiseError:
        _clean(dest, created)
        raise
    except OSError as exc:
        _clean(dest, created)
        if exc.errno == errno.ENOSPC:
            raise MaterialiseError(
                "no space left while copying the job's files",
                failure_type=FailureType.DISK_FULL,
            ) from None
        raise MaterialiseError(
            f"cannot copy the job's files ({type(exc).__name__}: "
            f"{exc.strerror or 'I/O error'})",
            failure_type=FailureType.ENV_ERROR,
        ) from None
    return stats


# ------------------------------------------------------------- subsets (V0.10)


@contextlib.contextmanager
def _writable(folder: Path, root: Path) -> Iterator[None]:
    """Let the agent create a file in ``folder`` (a read-only named-input
    folder of the workdir) for the duration of the block, then restore its
    mode. Only folders inside ``root``; POSIX only (Windows ignores it)."""
    real = Path(os.path.realpath(folder))
    if os.name != "posix" or not real.is_relative_to(root) or not real.is_dir():
        yield
        return
    mode = os.lstat(real).st_mode & 0o7777
    if mode & 0o200:
        yield
        return
    os.chmod(real, mode | 0o200)
    try:
        yield
    finally:
        os.chmod(real, mode)


def materialise_subset(
    store: CasStore,
    entries: Sequence[TreeEntry],
    dest: Path,
    *,
    free_disk_floor_bytes: int = 0,
    disk_free: Callable[[Path], int] | None = None,
) -> list[Path]:
    """V0.10 G4 (D264): copy the file ``entries`` (one chunk's items) from
    ``store`` into the **existing** workdir ``dest`` at their own paths, hashed
    while copying (a corrupt blob raises :class:`CorruptBlobError` and is
    evicted, like :func:`materialise`), each file read-only (``0o444``).

    Every path is created with ``O_EXCL | O_NOFOLLOW`` under real folders of
    ``dest`` (``_place`` refuses a folder that resolves outside it), so a file
    the chunk's job left in the way is refused (``invalid_input``), never
    overwritten or followed. Returns the files placed; on any failure the files
    this call placed are removed again. Blocking: run it in a thread."""
    dest = Path(dest)
    for entry in entries:
        if entry.kind != "file":
            raise MaterialiseError(
                "a chunk item is not a file",
                failure_type=FailureType.INVALID_INPUT,
                details={"path": entry.path[:200]},
            )
    needed = sum(e.size for e in entries)
    free = (disk_free or _disk_free)(dest)
    if free - needed < free_disk_floor_bytes:
        raise MaterialiseError(
            f"not enough disk for the chunk's files ({needed} bytes, keeping "
            f"{free_disk_floor_bytes} free)",
            failure_type=FailureType.DISK_FULL,
        )
    root = Path(os.path.realpath(dest))
    stats = MaterialiseStats()
    placed: list[Path] = []
    try:
        for entry in entries:
            # a missing folder under a read-only input folder: open the
            # deepest existing one while the missing ones are made
            parent = dest.joinpath(*entry.path.split("/")[:-1])
            existing = parent
            while not existing.exists() and existing != dest:
                existing = existing.parent
            with _writable(existing, root):
                path = _place(dest, entry.path)
            with _writable(path.parent, root):
                try:
                    _copy_file(store, entry, path, stats)
                except FileExistsError:
                    raise MaterialiseError(
                        "a chunk item's path is already taken in the workdir",
                        failure_type=FailureType.INVALID_INPUT,
                        details={"path": entry.path[:200]},
                    ) from None
                placed.append(path)
                if os.name == "posix":
                    os.chmod(path, 0o444)
    except BaseException:
        remove_subset(placed, dest)
        raise
    return placed


def remove_subset(paths: Sequence[Path], dest: Path) -> int:
    """Remove the files :func:`materialise_subset` placed (best effort);
    returns how many were removed."""
    root = Path(os.path.realpath(dest))
    removed = 0
    for path in paths:
        with contextlib.suppress(OSError), _writable(path.parent, root):
            path.unlink()
            removed += 1
    return removed
