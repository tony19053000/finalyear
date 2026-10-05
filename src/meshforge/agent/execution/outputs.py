"""Collect an attempt's declared outputs (V0.4 G5, D54/D74).

After the workload exits successfully the executor expands the manifest's
``outputs`` inside the attempt's workdir, packs what matched with
:func:`meshforge.common.bundle.pack_files` (the same determinism as a project
bundle, so the same files give the same digest on every node) and uploads the
archive with the attempt's upload grant.

**Matching.** A pattern is a relative POSIX path with glob wildcards: ``*`` and
``?`` inside one path component, ``[...]`` classes, and ``**`` for any number of
folders. A pattern naming a folder (``results`` or ``results/``) takes every
file below it. Matching is exact on the path from the workdir root - ``*.csv``
is top-level only, ``**/*.csv`` is any depth. ``.meshforge/`` (logs, installed
requirements) is never an output.

**Refused** (``output_error``, not retryable): a matched symlink - file or folder
(D47: nothing may lead outside the workdir) - or anything that is not a regular
file; more files or bytes than this attempt may upload.

**Missing** - a pattern that matched nothing - is *listed* in the terminal status
(``missing_outputs``), never an error. Nothing declared: no archive, no upload.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from meshforge.agent.execution.failure_evidence import DISK_FULL_ERRNOS
from meshforge.common.bundle import BundleError, BundleLimits, pack_files
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest

__all__ = [
    "INTERNAL_DIR",
    "MAX_SCANNED_ENTRIES",
    "Collected",
    "OutputError",
    "collect_outputs",
    "expand_outputs",
]

INTERNAL_DIR: Final = ".meshforge"
MAX_SCANNED_ENTRIES: Final = 1_000_000
"""Workdir entries examined at most while matching (a bound on a hostile tree)."""


class OutputError(MeshForgeError):
    default_failure_type = FailureType.OUTPUT_ERROR


@dataclass(frozen=True, slots=True)
class Collected:
    archive: Path | None
    """The packed outputs, or None when nothing matched / nothing was declared."""
    digest: Digest | None
    compressed_bytes: int
    files: int
    missing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Tree:
    files: tuple[str, ...]
    dirs: tuple[str, ...]
    links: frozenset[str]
    """Symlinks and other non-regular entries (never collectable)."""


def _scan(workdir: Path) -> _Tree:
    files: list[str] = []
    dirs: list[str] = []
    links: set[str] = set()
    seen = 0
    for top, dirnames, filenames in os.walk(workdir, followlinks=False):
        rel_top = Path(top).relative_to(workdir).as_posix()
        prefix = "" if rel_top == "." else rel_top + "/"
        if not prefix:
            dirnames[:] = [d for d in dirnames if d != INTERNAL_DIR]
            filenames = [f for f in filenames if f != INTERNAL_DIR]
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = prefix + name
            if os.path.islink(os.path.join(top, name)):
                links.add(rel)
            else:
                dirs.append(rel)
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = prefix + name
            mode = os.lstat(os.path.join(top, name)).st_mode
            if stat.S_ISREG(mode):
                files.append(rel)
            else:
                links.add(rel)
        seen += len(dirnames) + len(filenames)
        if seen > MAX_SCANNED_ENTRIES:
            raise OutputError(
                f"the work folder has more than {MAX_SCANNED_ENTRIES} entries",
                retryable=False,
            )
    return _Tree(tuple(files), tuple(dirs), frozenset(links))


def _match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match(rest, path[i:]) for i in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], head) and _match(rest, path[1:])


def _parts(pattern: str) -> tuple[str, ...]:
    text = pattern.strip().removeprefix("./").rstrip("/")
    return tuple(p for p in text.split("/") if p not in ("", "."))


def expand_outputs(
    workdir: Path, patterns: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """``(files, missing)``: the regular files the patterns select (sorted,
    unique, relative POSIX) and the patterns that matched nothing.

    Raises :class:`OutputError` when a pattern selects a symlink or a special
    file (see module docstring).
    """
    tree = _scan(workdir)
    selected: set[str] = set()
    missing: list[str] = []
    for pattern in patterns:
        parts = _parts(pattern)
        if not parts or ".." in parts or parts[0] == INTERNAL_DIR:
            missing.append(pattern)
            continue
        hit = False
        for rel in tree.links:
            if _match(parts, tuple(rel.split("/"))):
                raise OutputError(
                    f"declared output {pattern!r} matches a symbolic link or a "
                    f"special file ({rel}); only regular files are collected",
                    retryable=False,
                )
        matched_dirs = [d for d in tree.dirs if _match(parts, tuple(d.split("/")))]
        for rel in tree.files:
            if _match(parts, tuple(rel.split("/"))) or any(
                rel.startswith(d + "/") for d in matched_dirs
            ):
                selected.add(rel)
                hit = True
        for d in matched_dirs:
            for rel in tree.links:
                if rel.startswith(d + "/"):
                    raise OutputError(
                        f"declared output {pattern!r} contains a symbolic link or "
                        f"a special file ({rel}); only regular files are collected",
                        retryable=False,
                    )
            hit = True  # an empty matched folder is not "missing"
        if not hit:
            missing.append(pattern)
    return sorted(selected), missing


def collect_outputs(
    workdir: Path,
    patterns: tuple[str, ...],
    dest: Path,
    *,
    max_bytes: int,
    max_files: int = 100_000,
) -> Collected:
    """Expand, then pack into ``dest`` (blocking: run it in a thread)."""
    files, missing = expand_outputs(workdir, patterns)
    if not files:
        return Collected(None, None, 0, 0, tuple(missing))
    limits = BundleLimits(max_total_bytes=max_bytes, max_files=max_files)
    try:
        with dest.open("xb") as out:
            result = pack_files(workdir, files, out, limits=limits)
            out.flush()
            os.fsync(out.fileno())
    except BundleError as exc:
        dest.unlink(missing_ok=True)
        raise OutputError(
            f"cannot pack the outputs: {exc.message}", retryable=False
        ) from None
    except OSError as exc:
        dest.unlink(missing_ok=True)
        if exc.errno in DISK_FULL_ERRNOS:  # D116: typed, not an output_error
            raise OutputError(
                f"no disk space to pack the outputs ({exc.strerror})",
                failure_type=FailureType.DISK_FULL,
                retryable=False,
            ) from None
        raise OutputError(
            f"cannot pack the outputs ({exc.strerror or type(exc).__name__})",
            retryable=True,
        ) from None
    if result.compressed_bytes > max_bytes:
        dest.unlink(missing_ok=True)
        raise OutputError(
            f"packed outputs are {result.compressed_bytes} bytes; this attempt may "
            f"upload at most {max_bytes}",
            retryable=False,
        )
    return Collected(
        dest, result.digest, result.compressed_bytes, result.stats.files, tuple(missing)
    )
