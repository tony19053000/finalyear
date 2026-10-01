"""The project-folder walk shared by bundles and tree manifests (V0.5 G1, D81).

Extracted from :mod:`meshforge.common.bundle` so a **bundle** (D47) and a **tree
manifest** (D81) see exactly the same files: the same ignore rules and the same
path-safety refusals, written once.

**Ignored:** the built-ins (:data:`BUILTIN_IGNORES`), then every ``.gitignore`` and
``.computeignore`` in the tree, with git's semantics via ``pathspec``
(``GitIgnoreSpec``): a nested ignore file applies below its own folder, a later rule
(a deeper folder, or ``.computeignore`` after ``.gitignore``) overrides an earlier
one, and an ignored folder is not descended into.

**Refused** (the caller's error class, typed ``invalid_input``): symlinks whose
target leaves the tree, Windows junctions, devices/FIFOs/sockets, names that are
not UTF-8, paths over :data:`MAX_PATH_BYTES` / names over
:data:`MAX_COMPONENT_BYTES`, ``\\`` in a POSIX name, and trees over the caps.

Every refusal raises the ``error`` class the caller passes (``BundleError`` for
bundles, ``TreeError`` for trees) so each keeps its own public exception type.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final, Literal

import pathspec

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = [
    "BUILTIN_IGNORES",
    "IGNORE_FILE_NAMES",
    "MAX_COMPONENT_BYTES",
    "MAX_IGNORE_FILE_BYTES",
    "MAX_PATH_BYTES",
    "PathError",
    "ScanState",
    "WalkEntry",
    "check_rel",
    "open_regular",
    "scan_tree",
]

MAX_PATH_BYTES: Final = 4096
MAX_COMPONENT_BYTES: Final = 255
MAX_IGNORE_FILE_BYTES: Final = 1024 * 1024
BUILTIN_IGNORES: Final = (".git/", "__pycache__/", ".venv/", "node_modules/", "*.pyc")
IGNORE_FILE_NAMES: Final = (".gitignore", ".computeignore")
"""Read in this order in every folder; a later rule wins."""

WINDOWS: Final = sys.platform.startswith("win")
DRIVE_RE: Final = re.compile(r"[A-Za-z]:")
_GLOB_SPECIAL: Final = re.compile(r"([\[\]*?!#\\])")


class PathError(MeshForgeError, ValueError):
    """A tree cannot be walked/accepted. ``details["path"]`` = the relative path.

    The path is shown bounded and printable; never an absolute path on this
    machine. Subclassed by ``BundleError`` and ``TreeError``.
    """

    default_failure_type = FailureType.INVALID_INPUT

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        failure_type: FailureType | None = None,
    ) -> None:
        shown = shown_path(path) if path is not None else None
        super().__init__(
            f"{message}: {shown}" if shown else message,
            failure_type=failure_type,
            details={"path": shown} if shown else {},
        )


def shown_path(path: str) -> str:
    """A path for an error message: printable, at most 200 characters."""
    flat = "".join(ch if ch.isprintable() else "?" for ch in path)
    return flat if len(flat) <= 200 else flat[:197] + "..."


@dataclass(slots=True)
class WalkEntry:
    rel: str
    kind: Literal["file", "dir", "symlink"]
    path: Path
    size: int = 0
    executable: bool = False
    link: str = ""
    ident: tuple[int, int] = (0, 0)
    """``(st_dev, st_ino)`` seen when the file was listed; the opened file must
    be the same one (``0`` = unknown, e.g. ``scandir`` on Windows)."""


@dataclass(slots=True)
class ScanState:
    root: Path
    max_files: int
    max_total_bytes: int
    error: type[PathError]
    exclude: frozenset[str] = frozenset()
    entries: list[WalkEntry] = field(default_factory=list)
    total: int = 0
    ignored: int = 0
    what: str = "project"

    def add(self, entry: WalkEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.max_files:
            raise self.error(
                f"more than {self.max_files} files and folders "
                "(add rules to .computeignore, or raise the limit)"
            )
        self.total += entry.size
        if self.total > self.max_total_bytes:
            raise self.error(
                f"{self.what} is larger than {self.max_total_bytes} bytes "
                "(add rules to .computeignore, or raise the limit)"
            )


def _escape_glob(text: str) -> str:
    return _GLOB_SPECIAL.sub(r"\\\1", text)


def _rebase(line: str, rel_dir: str) -> str | None:
    """A gitignore line from the file in ``rel_dir`` rewritten relative to the root."""
    line = line.rstrip("\r\n")
    if not line.strip() or line.startswith("#"):
        return None
    if not rel_dir:
        return line
    negate = line.startswith("!")
    body = line[1:] if negate else line
    prefix = _escape_glob(rel_dir)
    # git: a pattern with a slash anywhere but at the end is anchored to the
    # folder of its .gitignore; otherwise it matches at any depth below it.
    anchored = "/" in body.rstrip("/")
    rebased = prefix + body.lstrip("/") if anchored else prefix + "**/" + body
    return ("!" if negate else "") + rebased


def _read_ignore_lines(
    directory: Path, rel_dir: str, error: type[PathError]
) -> list[str]:
    lines: list[str] = []
    for name in IGNORE_FILE_NAMES:
        path = directory / name
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise error(
                f"cannot read {name} ({exc.strerror or exc})", path=rel_dir + name
            ) from None
        if not stat.S_ISREG(st.st_mode):
            continue  # a symlinked/odd ignore file is not read (deterministic)
        if st.st_size > MAX_IGNORE_FILE_BYTES:
            raise error(
                f"{name} is larger than {MAX_IGNORE_FILE_BYTES} bytes",
                path=rel_dir + name,
            )
        try:
            text = path.read_bytes()[: MAX_IGNORE_FILE_BYTES + 1].decode("utf-8")
        except UnicodeDecodeError:
            raise error(f"{name} is not UTF-8", path=rel_dir + name) from None
        except OSError as exc:
            raise error(
                f"cannot read {name} ({exc.strerror or exc})", path=rel_dir + name
            ) from None
        for raw in text.removeprefix("﻿").splitlines():
            rebased = _rebase(raw, rel_dir)
            if rebased is not None:
                lines.append(rebased)
    return lines


def check_rel(rel: str, error: type[PathError]) -> None:
    """A listed relative path is portable UTF-8 and within the length caps."""
    try:
        encoded = rel.encode("utf-8")
    except UnicodeEncodeError:
        raise error(
            "a file name is not valid UTF-8 and cannot be bundled portably",
            path=rel.encode("utf-8", "replace").decode("utf-8"),
        ) from None
    if len(encoded) > MAX_PATH_BYTES:
        raise error(f"path longer than {MAX_PATH_BYTES} bytes", path=rel)
    if len(rel.rsplit("/", 1)[-1].encode("utf-8")) > MAX_COMPONENT_BYTES:
        raise error(f"name longer than {MAX_COMPONENT_BYTES} bytes", path=rel)
    if "\\" in rel and not WINDOWS:
        raise error("a file name contains '\\' (not portable)", path=rel)


def _link_target(path: Path, rel: str, root_real: str, error: type[PathError]) -> str:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise error(f"cannot read symlink ({exc.strerror or exc})", path=rel) from None
    if WINDOWS:
        target = target.removeprefix("\\\\?\\").replace("\\", "/")
    elif "\\" in target:
        raise error("symlink target contains '\\' (not portable)", path=rel)
    try:
        target_bytes = len(target.encode("utf-8"))
    except UnicodeEncodeError:
        raise error("symlink target is not valid UTF-8", path=rel) from None
    if not target or "\x00" in target or target_bytes > MAX_PATH_BYTES:
        raise error("symlink target is empty or too long", path=rel)
    if target.startswith("/") or DRIVE_RE.match(target):
        raise error("symlink target is absolute (it must stay inside)", path=rel)
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    if joined == ".." or joined.startswith("../"):
        raise error("symlink points outside the project folder", path=rel)
    real = os.path.realpath(path)
    if os.path.commonpath([real, root_real]) != root_real:
        raise error("symlink resolves outside the project folder", path=rel)
    return target


def scan_tree(
    root: Path,
    *,
    max_files: int,
    max_total_bytes: int,
    error: type[PathError],
    exclude: Iterable[str] = (),
    what: str = "project",
) -> ScanState:
    """List ``root`` (see module docstring); entries sorted by POSIX path."""
    state = ScanState(
        root=root,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        error=error,
        exclude=frozenset(exclude),
        what=what,
    )
    root_real = os.path.realpath(root)
    # Explicit stack (no recursion): a deep tree must not hit the recursion limit.
    stack: list[tuple[Path, str, list[str]]] = [(root, "", list(BUILTIN_IGNORES))]
    while stack:
        directory, rel_dir, inherited = stack.pop()
        rules = inherited + _read_ignore_lines(directory, rel_dir, error)
        spec = pathspec.GitIgnoreSpec.from_lines(rules)
        try:
            with os.scandir(directory) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise error(
                f"cannot list folder ({exc.strerror or exc})", path=rel_dir or "."
            ) from None
        for child in children:
            rel = rel_dir + child.name
            if rel in state.exclude:
                continue
            check_rel(rel, error)
            path = Path(child.path)
            if child.is_junction():
                if spec.match_file(rel + "/"):
                    state.ignored += 1
                    continue
                raise error("Windows junctions cannot be bundled", path=rel)
            if child.is_symlink():
                if spec.match_file(rel):
                    state.ignored += 1
                    continue
                link = _link_target(path, rel, root_real, error)
                state.add(WalkEntry(rel, "symlink", path, link=link))
            elif child.is_dir(follow_symlinks=False):
                if spec.match_file(rel + "/"):
                    state.ignored += 1
                    continue
                state.add(WalkEntry(rel, "dir", path))
                stack.append((path, rel + "/", rules))
            elif child.is_file(follow_symlinks=False):
                if spec.match_file(rel):
                    state.ignored += 1
                    continue
                st = child.stat(follow_symlinks=False)
                executable = (not WINDOWS) and bool(st.st_mode & stat.S_IXUSR)
                state.add(
                    WalkEntry(
                        rel,
                        "file",
                        path,
                        size=st.st_size,
                        executable=executable,
                        ident=(st.st_dev, st.st_ino),
                    )
                )
            else:
                if spec.match_file(rel):
                    state.ignored += 1
                    continue
                raise error("devices, FIFOs and sockets cannot be bundled", path=rel)
    state.entries.sort(key=lambda e: e.rel)
    return state


def open_regular(
    path: Path, rel: str, error: type[PathError], ident: tuple[int, int] = (0, 0)
) -> BinaryIO:
    """Open a listed file for reading, refusing anything swapped in since.

    ``O_NOFOLLOW`` refuses a symlink put at the name (POSIX; Windows has no such
    flag and would follow it) and ``O_NONBLOCK`` keeps a FIFO from blocking the
    open. Then the *open descriptor* is checked: it must be a regular file and -
    when the listing saw an inode - the same ``(st_dev, st_ino)``. That second
    check is the Windows equivalent of ``O_NOFOLLOW`` (a followed link opens a
    different file) and also catches a folder on the way swapped for a link.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise error(f"cannot read file ({exc.strerror or exc})", path=rel) from None
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise error(f"cannot read file ({exc.strerror or exc})", path=rel) from None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise error("file was replaced while packing", path=rel)
    if ident[1] and st.st_ino and (st.st_dev, st.st_ino) != ident:
        os.close(fd)
        raise error("file was replaced while packing", path=rel)
    return os.fdopen(fd, "rb")
