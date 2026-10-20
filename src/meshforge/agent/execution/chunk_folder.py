"""The per-chunk control folder of a lane (V0.10 G4, D260; fix r2 B1). Blocking:
run its methods in a thread.

**Where.** ``<work root>/<attempt_id>.chunk/`` - next to the lane's workdir,
never inside it: the job may rewrite anything in its workdir (Docker binds it
read-write at ``/work``), and a folder the agent later packs or deletes must
not be one the job can swap for a symlink to the agent's own files. It holds

* ``in/`` - the chunk's items file (``items.json`` / ``items.txt``); Docker
  binds it **read-only** at ``/meshforge/in``;
* ``out/`` - the empty folder whose files are the chunk's result; Docker binds
  it read-write at ``/meshforge/out``.

The unsandboxed runtimes see the host paths (they could reach them anyway,
as the agent's user - that is what unsandboxed means).

**Belt and braces.** The device + inode of the folder, ``in/`` and ``out/`` are
recorded when they are made; before ``out/`` is packed (and again after) every
one of them must still be that same real directory (``lstat``: not a link, same
identity) - a swap is refused (:class:`ChunkFolderSwappedError`, the chunk fails
``output_error``, not retried). Packing reads only regular files whose real
path stays inside ``out/`` (:func:`~meshforge.common.bundle.pack_files`).
Removing never follows a link out of the agent's work root: a link is unlinked,
never walked; a real folder is removed with ``shutil.rmtree`` (which does not
follow links inside it) only after its parent was checked to be the work root.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Final

from meshforge.agent.execution.outputs import OutputError, expand_outputs
from meshforge.agent.execution.runtime import ExtraMount
from meshforge.common.bundle import BundleError, BundleLimits, pack_files
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest

__all__ = [
    "CONTAINER_CHUNK_IN",
    "CONTAINER_CHUNK_OUT",
    "ITEMS_JSON",
    "ITEMS_LINES",
    "ChunkFolder",
    "ChunkFolderSwappedError",
]

CONTAINER_CHUNK_IN: Final = "/meshforge/in"
CONTAINER_CHUNK_OUT: Final = "/meshforge/out"
ITEMS_JSON: Final = "items.json"
ITEMS_LINES: Final = "items.txt"


class ChunkFolderSwappedError(OutputError):
    """The chunk's control folder is no longer the one the agent made."""

    default_failure_type = FailureType.OUTPUT_ERROR


def _ident(path: Path) -> tuple[int, int]:
    """``(dev, ino)`` of a real directory at ``path`` (never through a link)."""
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ChunkFolderSwappedError(
            "chunk_output_swapped: the chunk's control folder was replaced "
            "(not a real directory any more)",
            retryable=False,
        )
    return info.st_dev, info.st_ino


class ChunkFolder:
    """One chunk's control folder (see module docstring)."""

    def __init__(
        self,
        work_root: Path,
        attempt_id: str,
        *,
        lines: bool,
        docker: bool,
    ) -> None:
        self.work_root = work_root
        self.root = work_root / f"{attempt_id}.chunk"
        self.inbox = self.root / "in"
        self.out = self.root / "out"
        name = ITEMS_LINES if lines else ITEMS_JSON
        self.items = self.inbox / name
        self.docker = docker
        if docker:
            self.seen_items = str(PurePosixPath(CONTAINER_CHUNK_IN, name))
            self.seen_out = CONTAINER_CHUNK_OUT
        else:
            self.seen_items = str(self.items)
            self.seen_out = str(self.out)
        self._idents: dict[Path, tuple[int, int]] = {}

    @property
    def mounts(self) -> tuple[ExtraMount, ...]:
        """The Docker binds of this chunk (none for the other runtimes)."""
        if not self.docker:
            return ()
        return (
            ExtraMount(self.inbox, CONTAINER_CHUNK_IN, read_only=True),
            ExtraMount(self.out, CONTAINER_CHUNK_OUT, read_only=False),
        )

    # ------------------------------------------------------------ make / check

    def create(self) -> None:
        """A fresh, empty folder (whatever was left at its path is removed
        first, safely); identities recorded."""
        self.remove()
        self.root.mkdir(mode=0o700)
        self.inbox.mkdir(mode=0o755)
        # owner-only; the Docker runtime makes it the container user's
        # (prepare_workdir, like the workdir - never opened to everyone)
        self.out.mkdir(mode=0o700)
        for path in (self.root, self.inbox, self.out):
            self._idents[path] = _ident(path)

    def verify(self) -> None:
        """Every recorded folder is still the same real directory."""
        if not self._idents:
            raise ChunkFolderSwappedError(
                "chunk_output_swapped: the chunk's control folder was never made",
                retryable=False,
            )
        for path, ident in self._idents.items():
            try:
                now = _ident(path)
            except OSError:
                now = (-1, -1)
            if now != ident:
                raise ChunkFolderSwappedError(
                    "chunk_output_swapped: the chunk's control folder was replaced "
                    "while it ran; its output is not used",
                    retryable=False,
                )

    # ------------------------------------------------------------ pack

    def pack_out(self, dest: Path, max_bytes: int) -> tuple[Digest, int]:
        """Pack every regular file of ``out/`` (the deterministic bundle of
        V0.4 outputs; an empty folder is an empty bundle), refusing a swapped
        folder before and after."""
        self.verify()
        dest.unlink(missing_ok=True)
        try:
            files, _missing = expand_outputs(self.out, ("**",))
            with dest.open("xb") as fh:
                packed = pack_files(
                    self.out, files, fh, limits=BundleLimits(max_total_bytes=max_bytes)
                )
        except BundleError as exc:
            dest.unlink(missing_ok=True)
            raise OutputError(
                f"cannot pack the chunk's output: {exc.message}", retryable=False
            ) from None
        try:
            self.verify()
        except ChunkFolderSwappedError:
            dest.unlink(missing_ok=True)
            raise
        if packed.compressed_bytes > max_bytes:
            dest.unlink(missing_ok=True)
            raise OutputError(
                f"output_limit: the chunk's packed output is "
                f"{packed.compressed_bytes} bytes, more than "
                f"split.max_chunk_output_bytes ({max_bytes})",
                retryable=False,
            )
        return packed.digest, packed.compressed_bytes

    # ------------------------------------------------------------ remove

    def remove(self) -> None:
        """Remove the folder without ever following a link out of the work
        root: a link at its path is unlinked (its target untouched); a real
        folder is removed only when its parent is the work root itself."""
        path = self.root
        with contextlib.suppress(FileNotFoundError):
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                path.unlink()  # a link or a file: the entry only
                return
            if os.path.realpath(path.parent) != os.path.realpath(self.work_root):
                return  # pragma: no cover - the work root itself was moved
            shutil.rmtree(path, ignore_errors=True)
        self._idents.clear()
