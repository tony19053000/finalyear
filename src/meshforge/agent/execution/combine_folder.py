"""The control folder of a split job's combine (V0.10 G5, D268/D283 pin 4: the
G4 fix r2 B1 rules). Blocking: run its methods in a thread.

**Where.** ``<work root>/<attempt_id>.combine/`` - next to the attempt's
workdir, never inside it (a combine script may rewrite anything in its
workdir; a folder the agent unpacks into, packs or deletes must not be one the
job can swap for a symlink). It holds

* ``parts/NNNNN/`` - chunk ``N``'s committed result, unpacked (hostile-input
  safe) in **chunk-index order**, the width ``max(5, digits of the chunk
  count)`` so that name order is chunk order; every part (and ``parts/``
  itself) is made **read-only** once unpacked. Docker binds it read-only at
  ``/meshforge/parts``;
* ``out/`` - the empty folder whose files become the job's output. Docker binds
  it read-write at ``/meshforge/out``;
* ``result.json`` - the built-in combine's answer (only the agent's own
  process writes it).

The unsandboxed runtimes see the host paths.

**Belt and braces** (as :mod:`.chunk_folder`): the device + inode of the
folder, ``parts/`` and ``out/`` are recorded when they are made; before
``out/`` is packed (and again after) each must still be that same real
directory - a swap fails the combine ``output_error`` (``combine_output_
swapped``), not retried. Packing reads only regular files whose real path stays
inside ``out/``. Removing never follows a link out of the work root.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from meshforge.agent.envs.fsutil import make_read_only, remove_tree
from meshforge.agent.execution.chunk_folder import pack_folder
from meshforge.agent.execution.outputs import OutputError
from meshforge.agent.execution.runtime import ExtraMount
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger

__all__ = [
    "CONTAINER_COMBINE_OUT",
    "CONTAINER_PARTS",
    "CombineFolder",
    "CombineFolderSwappedError",
    "combine_argv",
    "combine_env",
    "part_width",
]

CONTAINER_PARTS: Final = "/meshforge/parts"
CONTAINER_COMBINE_OUT: Final = "/meshforge/out"
_PLACEHOLDER: Final = re.compile(r"\{(parts|out)\}")

log = get_logger(__name__)


class CombineFolderSwappedError(OutputError):
    """The combine's control folder is no longer the one the agent made."""

    default_failure_type = FailureType.OUTPUT_ERROR


def part_width(chunks: int) -> int:
    """Digits of a part folder's name: ``max(5, digits of the chunk count)``."""
    return max(5, len(str(max(chunks, 1))))


def combine_argv(argv: Sequence[str], *, parts: str, out: str) -> tuple[str, ...]:
    """``{parts}`` / ``{out}`` replaced once, left to right, inside any argv
    item; a value is never scanned again; other ``{...}`` text stays."""
    values = {"parts": parts, "out": out}
    return tuple(_PLACEHOLDER.sub(lambda m: values[m.group(1)], arg) for arg in argv)


def combine_env(env: Mapping[str, str], *, parts: str, out: str) -> dict[str, str]:
    merged = dict(env)
    merged["MESHFORGE_COMBINE_PARTS"] = parts
    merged["MESHFORGE_COMBINE_OUT"] = out
    return merged


def _ident(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise CombineFolderSwappedError(
            "combine_output_swapped: the combine's control folder was replaced "
            "(not a real directory any more)",
            retryable=False,
        )
    return info.st_dev, info.st_ino


class CombineFolder:
    """One combine's control folder (see module docstring)."""

    def __init__(
        self, work_root: Path, attempt_id: str, *, chunks: int, docker: bool
    ) -> None:
        self.work_root = work_root
        self.root = work_root / f"{attempt_id}.combine"
        self.parts = self.root / "parts"
        self.out = self.root / "out"
        self.result = self.root / "result.json"
        self.chunks = chunks
        self.width = part_width(chunks)
        self.docker = docker
        if docker:
            self.seen_parts = CONTAINER_PARTS
            self.seen_out = CONTAINER_COMBINE_OUT
        else:
            self.seen_parts = str(self.parts)
            self.seen_out = str(self.out)
        self._idents: dict[Path, tuple[int, int]] = {}

    @property
    def mounts(self) -> tuple[ExtraMount, ...]:
        """The Docker binds of a combine script (none for the other runtimes)."""
        if not self.docker:
            return ()
        return (
            ExtraMount(self.parts, CONTAINER_PARTS, read_only=True),
            ExtraMount(self.out, CONTAINER_COMBINE_OUT, read_only=False),
        )

    def part(self, index: int) -> Path:
        """Where chunk ``index``'s result is unpacked."""
        if not 0 <= index < self.chunks:
            raise ValueError("the chunk index is out of range")
        return self.parts / f"{index:0{self.width}d}"

    # ------------------------------------------------------------ make / check

    def create(self) -> None:
        """A fresh, empty folder (whatever was at its path is removed first,
        safely); identities recorded."""
        self.remove()
        self.root.mkdir(mode=0o700)
        # readable by the container's user (bound read-only); never writable
        # by it - unpacked parts lose their write bits (seal_part / seal)
        self.parts.mkdir(mode=0o755)
        self.out.mkdir(mode=0o700)
        for path in (self.root, self.parts, self.out):
            self._idents[path] = _ident(path)

    def verify(self) -> None:
        """Every recorded folder is still the same real directory."""
        if not self._idents:
            raise CombineFolderSwappedError(
                "combine_output_swapped: the combine's control folder was never made",
                retryable=False,
            )
        for path, ident in self._idents.items():
            try:
                now = _ident(path)
            except OSError:
                now = (-1, -1)
            if now != ident:
                raise CombineFolderSwappedError(
                    "combine_output_swapped: the combine's control folder was "
                    "replaced while it ran; its output is not used",
                    retryable=False,
                )

    def has_part(self, index: int) -> bool:
        """Chunk ``index`` was unpacked and sealed (a staging retry skips it)."""
        path = self.part(index)
        with contextlib.suppress(OSError):
            info = os.lstat(path)
            return stat.S_ISDIR(info.st_mode) and not info.st_mode & stat.S_IWUSR
        return False

    def drop_part(self, index: int) -> None:
        """A half-unpacked part goes before it is unpacked again."""
        remove_tree(self.part(index))

    def seal_part(self, index: int) -> None:
        """The unpacked part becomes read-only (files and folders)."""
        make_read_only(self.part(index))

    def seal(self) -> None:
        """Every part is in: ``parts/`` itself becomes read-only too."""
        self.verify()
        make_read_only(self.parts)

    # ------------------------------------------------------------ pack

    def pack_out(self, dest: Path, max_bytes: int) -> tuple[Digest, int]:
        """Pack ``out/`` (refusing a swapped folder before and after)."""
        self.verify()
        digest, size = pack_folder(
            self.out,
            dest,
            max_bytes,
            what="combine",
            limit="the job's max_output_bytes",
        )
        try:
            self.verify()
        except CombineFolderSwappedError:
            dest.unlink(missing_ok=True)
            raise
        return digest, size

    # ------------------------------------------------------------ remove

    def remove(self) -> None:
        """Remove the folder without ever following a link out of the work
        root: a link at its path is unlinked (its target untouched); a real
        folder - read-only parts included - only when its parent is the work
        root itself."""
        path = self.root
        with contextlib.suppress(FileNotFoundError):
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                path.unlink()
                return
            if os.path.realpath(path.parent) != os.path.realpath(self.work_root):
                # the work root itself was moved or replaced: never delete
                # through it; the folder is left for the operator
                log.warning(
                    "agent.combine.folder_left",
                    path=str(path),
                    work_root=str(self.work_root),
                    reason="work_root_moved",
                )
                return
            with contextlib.suppress(OSError):
                remove_tree(path)
            if os.path.lexists(path):
                # something still writes into it (a process that outlived the
                # combine): move it aside, the next combine gets a fresh path
                aside = path.with_name(f"{path.name}.stale-{uuid.uuid4().hex}")
                with contextlib.suppress(OSError):
                    path.rename(aside)
                    remove_tree(aside)
        self._idents.clear()
