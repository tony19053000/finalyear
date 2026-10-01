"""Filesystem helpers for environments: the build lock, read-only trees, removal.

**Lock.** One build per env hash per node, across processes (two agents on one
laptop, a ``prepare`` next to a running agent) and inside one process (two
attempts at once): :func:`env_lock` holds :func:`meshforge.common.filelock.locked`
on ``locks/<hash>.lock``. It is acquired by non-blocking attempts with an
``asyncio.sleep`` between them, so waiting never blocks the event loop and is
cancellable; the OS drops the lock when a holder dies (a killed build never
wedges the next one). Lock files are never deleted (see ``filelock``).

**Read-only.** A finished environment is made read-only before it becomes
visible: files and directories lose every write bit. That stops a
job from (accidentally or not) changing an environment the next job reuses. It is
a permission guard, not a sandbox: the unsandboxed runtimes run as the agent's
user, who could ``chmod`` it back (documented in OPERATIONS); Docker envs are
read-only by the container's read-only root filesystem instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import stat
import sys
import time
from collections.abc import AsyncIterator, Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.agent.envs.errors import EnvError
from meshforge.common.errors import FailureType
from meshforge.common.filelock import locked

__all__ = [
    "TreeSummary",
    "env_lock",
    "make_read_only",
    "remove_tree",
    "tree_size",
    "tree_summary",
]

_POLL_S: Final = 0.05
_WRITE_BITS: Final = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


@contextlib.asynccontextmanager
async def env_lock(
    path: Path,
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    poll_s: float = _POLL_S,
) -> AsyncIterator[None]:
    """Hold the exclusive build lock ``path`` (module docstring) until the block
    ends. ``EnvError(env_timeout)`` when ``deadline`` passes first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stack = contextlib.ExitStack()
    while True:
        try:
            stack.enter_context(locked(path, timeout_s=0.0))
        except TimeoutError:
            if clock() >= deadline:
                raise EnvError(
                    "another build of this environment did not finish within the "
                    "environment budget",
                    reason="env_timeout",
                    failure_type=FailureType.TIMEOUT,
                ) from None
            await asyncio.sleep(poll_s)
            continue
        except OSError as exc:
            raise EnvError(
                f"cannot open the environment lock: {exc.strerror or type(exc).__name__}",
                reason="env_lock_failed",
                failure_type=FailureType.INTERNAL_ERROR,
            ) from None
        break
    with stack:
        yield


def make_read_only(root: Path) -> None:
    """Remove every write bit under ``root`` (files first, then directories,
    deepest first). Symlinks are left alone (their mode is not theirs)."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                _drop_write(path)
        if sys.platform != "win32":  # Windows ignores the read-only bit on dirs
            for name in dirnames:
                path = os.path.join(dirpath, name)
                if not os.path.islink(path):
                    _drop_write(path)
    if sys.platform != "win32":
        _drop_write(str(root))


def _drop_write(path: str) -> None:
    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~_WRITE_BITS)


def _writable_again(func: Callable[..., object], path: str, exc: BaseException) -> None:
    """``rmtree`` error hook: a read-only entry (or its read-only parent) blocks
    removal - make both writable and retry once."""
    if not isinstance(exc, OSError):
        raise exc
    parent = os.path.dirname(path)
    with contextlib.suppress(OSError):
        os.chmod(parent, stat.S_IRWXU)
    with contextlib.suppress(OSError):
        if not os.path.islink(path):
            os.chmod(path, stat.S_IRWXU)
    func(path)


def remove_tree(path: Path) -> None:
    """Remove ``path`` (a file, a symlink or a whole tree, read-only or not).
    Missing is fine."""
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRWXU)
    shutil.rmtree(path, onexc=_writable_again)


def tree_size(root: Path) -> int:
    """Bytes of every regular file under ``root`` (symlinks not followed)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            with contextlib.suppress(OSError):
                info = os.lstat(os.path.join(dirpath, name))
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


@dataclass(frozen=True, slots=True)
class TreeSummary:
    """A cheap fingerprint of a tree: entry count, total bytes and the sha256 of the
    sorted ``path<NUL>type<NUL>size`` listing. Costs one ``lstat`` per entry (no
    file is read): it catches a deleted, added, renamed or resized file, not a
    same-size in-place edit."""

    files: int
    bytes: int
    listing_sha256: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "listing_sha256": self.listing_sha256,
        }


def tree_summary(root: Path, *, skip_root_names: Collection[str] = ()) -> TreeSummary:
    """:class:`TreeSummary` of ``root``. Left out, and nothing else: files named in
    ``skip_root_names`` **directly in** ``root`` (the marker), and ``*.pyc`` files
    **directly in** a ``__pycache__`` directory (an interpreter may write bytecode
    where the OS lets it, e.g. on Windows). Symlinks are not followed."""
    entries: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        at_root = rel_dir == "."
        in_bytecode_dir = os.path.basename(dirpath) == "__pycache__" and not at_root
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        for name in [*filenames, *links]:
            if at_root and name in skip_root_names:
                continue
            if in_bytecode_dir and name.endswith(".pyc") and name not in links:
                continue
            info = os.lstat(os.path.join(dirpath, name))
            kind = "l" if stat.S_ISLNK(info.st_mode) else "f"
            rel = name if at_root else f"{rel_dir}/{name}"
            entries.append(f"{rel.replace(os.sep, '/')}\0{kind}\0{info.st_size}")
            if kind == "f":
                total += info.st_size
    entries.sort()
    digest = hashlib.sha256("\n".join(entries).encode("utf-8", "surrogatepass"))
    return TreeSummary(len(entries), total, digest.hexdigest())
