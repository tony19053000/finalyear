"""Project bundles: deterministic pack, hostile-input-safe unpack (brief D47).

A **bundle** is a PAX tar of a project folder, compressed with zstd (level 3); the
SHA-256 of the *compressed* bytes is the **bundle digest** - the name the blob store
keeps it under (ADR-0003). The CLI packs, the agent unpacks, and output collection
(G5) reuses both: :func:`pack_files` packs an explicit selection of files (the
declared outputs) with the same determinism and no ignore rules - a file the
user declared as an output is never dropped by a ``.gitignore``.

**Deterministic.** The same tree gives the same bytes on Linux and Windows, so the
digest names the *content*, not the machine or the moment it was packed:

* entries sorted by their POSIX relative path (code-point order), ``/`` separators;
* uid/gid 0, empty uname/gname, mtime 0, no xattrs/ACLs, PAX format;
* modes normalised: directories ``0755``, files ``0644`` or ``0755`` (the owner exec
  bit is the only mode bit kept), symlinks ``0777``. Windows has no exec bit, so a
  file packed there is always ``0644`` (a script that is executable on Linux
  therefore packs to a different digest on Windows - documented, not hidden);
* zstd level 3, single-threaded, one frame, checksum on. The digest is stable for
  one ``zstandard`` build; a zstd upgrade *may* change it (V0.5's "resubmit sends 0
  bytes" then costs one re-upload). ``tests/unit/common/test_common_bundle.py``
  pins a golden digest that CI checks on Linux and Windows.

**Ignored:** the built-ins (:data:`BUILTIN_IGNORES`), then every ``.gitignore`` and
``.computeignore`` in the tree, with git's semantics via ``pathspec``
(``GitIgnoreSpec``): a nested ignore file applies below its own folder, a later rule
(a deeper folder, or ``.computeignore`` after ``.gitignore``) overrides an earlier
one, and an ignored folder is not descended into (git cannot re-include a file under
an excluded folder either).

**Refused at pack** (``invalid_input``): symlinks whose target leaves the tree,
Windows junctions, devices/FIFOs/sockets, names that are not UTF-8, paths over
:data:`MAX_PATH_BYTES`, and trees over the caps (:class:`BundleLimits`). A file
with several hard links is packed as an ordinary file (its content, once per
path); the archive never contains a hard-link entry.

**Unpack treats the archive as hostile** (it arrives over the network, from
whoever held the fabric token). Every member is checked *before* anything is
written: no absolute paths or drive letters, no ``..``/``.``/empty components, no
backslashes, no NUL; only regular files, directories and symlinks (hard links,
devices, FIFOs and sparse files are refused); symlink targets must stay inside the
destination - lexically at once and by real resolution at the end; no member may
live under a symlink or replace an earlier member; file count, declared sizes,
PAX/long-name metadata and the *decompressed* stream are all bounded (a
decompression bomb stops at the bound, not at the disk). Files are created with
``O_EXCL`` (and ``O_NOFOLLOW`` on POSIX) inside a destination that must be new or
empty. On any failure the destination's contents are removed and
:class:`BundleError` is raised - nothing is ever written outside it.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import posixpath
import re
import secrets
import shutil
import stat
import sys
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, BinaryIO, Final, Literal, cast

import pathspec
import zstandard

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest

__all__ = [
    "BUILTIN_IGNORES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "IGNORE_FILE_NAMES",
    "MAX_COMPONENT_BYTES",
    "MAX_IGNORE_FILE_BYTES",
    "MAX_META_BYTES",
    "MAX_PATH_BYTES",
    "ZSTD_LEVEL",
    "BundleError",
    "BundleLimits",
    "BundleStats",
    "PackResult",
    "check_member_name",
    "pack",
    "pack_files",
    "pack_to_file",
    "unpack",
]

DEFAULT_MAX_TOTAL_BYTES: Final = 1024**3
"""1 GiB of uncompressed file content per bundle (a setting on each side)."""
DEFAULT_MAX_FILES: Final = 100_000
"""Entries (files + folders + symlinks) per bundle."""
MAX_PATH_BYTES: Final = 4096
MAX_COMPONENT_BYTES: Final = 255
MAX_IGNORE_FILE_BYTES: Final = 1024 * 1024
MAX_META_BYTES: Final = 16 * 1024
"""Largest PAX extended header / GNU long name accepted on unpack. Our packer
writes at most a path and a link target (each <= 4 KiB) there; tarfile reads the
whole header into memory, so an unbounded one would be a memory bomb."""
ZSTD_LEVEL: Final = 3
MAX_WINDOW_SIZE: Final = 1 << 24
"""Decompression window cap (16 MiB). Level 3 uses <= 2 MiB; a hostile frame
asking for more is refused instead of allocating it."""
BUILTIN_IGNORES: Final = (".git/", "__pycache__/", ".venv/", "node_modules/", "*.pyc")
IGNORE_FILE_NAMES: Final = (".gitignore", ".computeignore")
"""Read in this order in every folder; a later rule wins."""

_COPY_CHUNK: Final = 1024 * 1024
_PER_MEMBER_OVERHEAD: Final = 3 * tarfile.BLOCKSIZE + MAX_META_BYTES
_FIXED_OVERHEAD: Final = 4 * tarfile.RECORDSIZE
_WINDOWS: Final = sys.platform.startswith("win")
_DRIVE_RE: Final = re.compile(r"[A-Za-z]:")
_WINDOWS_BAD_CHARS: Final = frozenset('<>:"|?*')
_WINDOWS_RESERVED: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_GLOB_SPECIAL: Final = re.compile(r"([\[\]*?!#\\])")
_META_TYPES: Final = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)


class BundleError(MeshForgeError, ValueError):
    """The tree cannot be packed, or the archive must not be unpacked.

    ``details["path"]`` is the offending *relative* POSIX path when there is one
    (bounded; never an absolute path on this machine).
    """

    default_failure_type = FailureType.INVALID_INPUT

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        failure_type: FailureType | None = None,
    ) -> None:
        shown = _shown(path) if path is not None else None
        super().__init__(
            f"{message}: {shown}" if shown else message,
            failure_type=failure_type,
            details={"path": shown} if shown else {},
        )


def _shown(path: str) -> str:
    """A path for an error message: printable, at most 200 characters."""
    flat = "".join(ch if ch.isprintable() else "?" for ch in path)
    return flat if len(flat) <= 200 else flat[:197] + "..."


@dataclass(frozen=True, slots=True)
class BundleLimits:
    """Caps applied by both :func:`pack` and :func:`unpack`."""

    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_decompressed_bytes: int | None = None
    """Bound on the decompressed tar stream; ``None`` = derived from the two caps
    above plus per-entry header overhead (:attr:`stream_bound`)."""

    def __post_init__(self) -> None:
        if self.max_total_bytes < 0 or self.max_files < 1:
            raise ValueError("bundle limits must be positive")

    @property
    def stream_bound(self) -> int:
        if self.max_decompressed_bytes is not None:
            return self.max_decompressed_bytes
        return (
            self.max_total_bytes
            + (self.max_files + 2) * _PER_MEMBER_OVERHEAD
            + _FIXED_OVERHEAD
        )


@dataclass(frozen=True, slots=True)
class BundleStats:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    total_bytes: int = 0
    """Uncompressed size of all regular files."""
    ignored: int = 0
    """Entries skipped by ignore rules (pack only; an ignored folder counts once)."""


@dataclass(frozen=True, slots=True)
class PackResult:
    digest: Digest
    """SHA-256 of the compressed bytes: the bundle's name in the blob store."""
    compressed_bytes: int
    stats: BundleStats


# =========================================================================== pack


@dataclass(slots=True)
class _Entry:
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
class _ScanState:
    root: Path
    limits: BundleLimits
    exclude: frozenset[str]
    entries: list[_Entry] = field(default_factory=list)
    total: int = 0
    ignored: int = 0


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


def _read_ignore_lines(directory: Path, rel_dir: str) -> list[str]:
    lines: list[str] = []
    for name in IGNORE_FILE_NAMES:
        path = directory / name
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BundleError(
                f"cannot read {name} ({exc.strerror or exc})", path=rel_dir + name
            ) from None
        if not stat.S_ISREG(st.st_mode):
            continue  # a symlinked/odd ignore file is not read (deterministic)
        if st.st_size > MAX_IGNORE_FILE_BYTES:
            raise BundleError(
                f"{name} is larger than {MAX_IGNORE_FILE_BYTES} bytes",
                path=rel_dir + name,
            )
        try:
            text = path.read_bytes()[: MAX_IGNORE_FILE_BYTES + 1].decode("utf-8")
        except UnicodeDecodeError:
            raise BundleError(f"{name} is not UTF-8", path=rel_dir + name) from None
        except OSError as exc:
            raise BundleError(
                f"cannot read {name} ({exc.strerror or exc})", path=rel_dir + name
            ) from None
        for raw in text.removeprefix("﻿").splitlines():
            rebased = _rebase(raw, rel_dir)
            if rebased is not None:
                lines.append(rebased)
    return lines


def _check_rel(rel: str) -> None:
    try:
        encoded = rel.encode("utf-8")
    except UnicodeEncodeError:
        raise BundleError(
            "a file name is not valid UTF-8 and cannot be bundled portably",
            path=rel.encode("utf-8", "replace").decode("utf-8"),
        ) from None
    if len(encoded) > MAX_PATH_BYTES:
        raise BundleError(f"path longer than {MAX_PATH_BYTES} bytes", path=rel)
    if len(rel.rsplit("/", 1)[-1].encode("utf-8")) > MAX_COMPONENT_BYTES:
        raise BundleError(f"name longer than {MAX_COMPONENT_BYTES} bytes", path=rel)
    if "\\" in rel and not _WINDOWS:
        raise BundleError("a file name contains '\\' (not portable)", path=rel)


def _link_target(path: Path, rel: str, root_real: str) -> str:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise BundleError(
            f"cannot read symlink ({exc.strerror or exc})", path=rel
        ) from None
    if _WINDOWS:
        target = target.removeprefix("\\\\?\\").replace("\\", "/")
    elif "\\" in target:
        raise BundleError("symlink target contains '\\' (not portable)", path=rel)
    if not target or "\x00" in target or len(target.encode()) > MAX_PATH_BYTES:
        raise BundleError("symlink target is empty or too long", path=rel)
    if target.startswith("/") or _DRIVE_RE.match(target):
        raise BundleError("symlink target is absolute (it must stay inside)", path=rel)
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    if joined == ".." or joined.startswith("../"):
        raise BundleError("symlink points outside the project folder", path=rel)
    real = os.path.realpath(path)
    if os.path.commonpath([real, root_real]) != root_real:
        raise BundleError("symlink resolves outside the project folder", path=rel)
    return target


def _add(state: _ScanState, entry: _Entry) -> None:
    state.entries.append(entry)
    if len(state.entries) > state.limits.max_files:
        raise BundleError(
            f"more than {state.limits.max_files} files and folders "
            "(add rules to .computeignore, or raise the limit)"
        )
    state.total += entry.size
    if state.total > state.limits.max_total_bytes:
        raise BundleError(
            f"project is larger than {state.limits.max_total_bytes} bytes "
            "(add rules to .computeignore, or raise the limit)"
        )


def _scan(root: Path, limits: BundleLimits, exclude: frozenset[str]) -> _ScanState:
    state = _ScanState(root=root, limits=limits, exclude=exclude)
    root_real = os.path.realpath(root)
    # Explicit stack (no recursion): a deep tree must not hit the recursion limit.
    stack: list[tuple[Path, str, list[str]]] = [(root, "", list(BUILTIN_IGNORES))]
    while stack:
        directory, rel_dir, inherited = stack.pop()
        rules = inherited + _read_ignore_lines(directory, rel_dir)
        spec = pathspec.GitIgnoreSpec.from_lines(rules)
        try:
            with os.scandir(directory) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise BundleError(
                f"cannot list folder ({exc.strerror or exc})", path=rel_dir or "."
            ) from None
        for child in children:
            rel = rel_dir + child.name
            if rel in exclude:
                continue
            _check_rel(rel)
            path = Path(child.path)
            if child.is_junction():
                if spec.match_file(rel + "/"):
                    state.ignored += 1
                    continue
                raise BundleError("Windows junctions cannot be bundled", path=rel)
            if child.is_symlink():
                if spec.match_file(rel):
                    state.ignored += 1
                    continue
                link = _link_target(path, rel, root_real)
                _add(state, _Entry(rel, "symlink", path, link=link))
            elif child.is_dir(follow_symlinks=False):
                if spec.match_file(rel + "/"):
                    state.ignored += 1
                    continue
                _add(state, _Entry(rel, "dir", path))
                stack.append((path, rel + "/", rules))
            elif child.is_file(follow_symlinks=False):
                if spec.match_file(rel):
                    state.ignored += 1
                    continue
                st = child.stat(follow_symlinks=False)
                executable = (not _WINDOWS) and bool(st.st_mode & stat.S_IXUSR)
                _add(
                    state,
                    _Entry(
                        rel,
                        "file",
                        path,
                        size=st.st_size,
                        executable=executable,
                        ident=(st.st_dev, st.st_ino),
                    ),
                )
            else:
                if spec.match_file(rel):
                    state.ignored += 1
                    continue
                raise BundleError(
                    "devices, FIFOs and sockets cannot be bundled", path=rel
                )
    state.entries.sort(key=lambda e: e.rel)
    return state


class _HashingWriter:
    """Counts and hashes every byte on its way to ``raw``."""

    def __init__(self, raw: IO[bytes]) -> None:
        self._raw = raw
        self.hasher = hashlib.sha256()
        self.count = 0

    def write(self, data: bytes) -> int:
        self.hasher.update(data)
        self.count += len(data)
        self._raw.write(data)
        return len(data)

    def flush(self) -> None:
        self._raw.flush()


def _tarinfo(entry: _Entry) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry.rel)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if entry.kind == "dir":
        info.type, info.mode = tarfile.DIRTYPE, 0o755
    elif entry.kind == "symlink":
        info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, entry.link
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o755 if entry.executable else 0o644
        info.size = entry.size
    return info


def _open_regular(path: Path, rel: str, ident: tuple[int, int] = (0, 0)) -> BinaryIO:
    """Open a listed file for packing, refusing anything swapped in since.

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
        raise BundleError(
            f"cannot read file ({exc.strerror or exc})", path=rel
        ) from None
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise BundleError(
            f"cannot read file ({exc.strerror or exc})", path=rel
        ) from None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise BundleError("file was replaced while packing", path=rel)
    if ident[1] and st.st_ino and (st.st_dev, st.st_ino) != ident:
        os.close(fd)
        raise BundleError("file was replaced while packing", path=rel)
    return os.fdopen(fd, "rb")


def pack(
    root: Path,
    out: IO[bytes],
    *,
    limits: BundleLimits | None = None,
    exclude: Iterable[str] = (),
) -> PackResult:
    """Write the bundle of ``root`` to ``out``; return its digest and stats.

    ``exclude`` lists relative POSIX paths to leave out (e.g. the output file
    itself when it is written inside the tree). Raises :class:`BundleError`.
    """
    limits = limits or BundleLimits()
    root = Path(root).resolve()
    if not root.is_dir():
        raise BundleError("the project folder does not exist or is not a folder")
    state = _scan(root, limits, frozenset(exclude))
    return _pack_entries(state, out)


def pack_files(
    root: Path,
    rels: Iterable[str],
    out: IO[bytes],
    *,
    limits: BundleLimits | None = None,
) -> PackResult:
    """Write a bundle of exactly the regular files ``rels`` (relative POSIX paths
    under ``root``) and their parent folders; no ignore rules.

    Every path must be a regular file whose real location is inside ``root`` - a
    symlink (the file itself or a folder on its way) is refused, like anything
    that is not a regular file. Raises :class:`BundleError`.
    """
    limits = limits or BundleLimits()
    root = Path(root).resolve()
    root_real = os.path.realpath(root)
    state = _ScanState(root=root, limits=limits, exclude=frozenset())
    dirs: set[str] = set()
    for rel in sorted(set(rels)):
        parts = check_member_name(rel)
        rel = "/".join(parts)
        _check_rel(rel)
        path = root.joinpath(*parts)
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in dirs:
                continue
            if root.joinpath(*parts[:depth]).is_symlink():
                raise BundleError("an output folder is a symbolic link", path=parent)
            dirs.add(parent)
            _add(state, _Entry(parent, "dir", root.joinpath(*parts[:depth])))
        try:
            st = path.lstat()
        except OSError as exc:
            raise BundleError(
                f"cannot read output ({exc.strerror or exc})", path=rel
            ) from None
        if stat.S_ISLNK(st.st_mode):
            raise BundleError("an output is a symbolic link", path=rel)
        if not stat.S_ISREG(st.st_mode):
            raise BundleError("an output is not a regular file", path=rel)
        if os.path.commonpath([os.path.realpath(path), root_real]) != root_real:
            raise BundleError("an output resolves outside the work folder", path=rel)
        executable = (not _WINDOWS) and bool(st.st_mode & stat.S_IXUSR)
        _add(
            state,
            _Entry(
                rel,
                "file",
                path,
                size=st.st_size,
                executable=executable,
                ident=(st.st_dev, st.st_ino),
            ),
        )
    state.entries.sort(key=lambda e: e.rel)
    return _pack_entries(state, out)


def _pack_entries(state: _ScanState, out: IO[bytes]) -> PackResult:
    writer = _HashingWriter(out)
    stats = BundleStats(
        files=sum(1 for e in state.entries if e.kind == "file"),
        dirs=sum(1 for e in state.entries if e.kind == "dir"),
        symlinks=sum(1 for e in state.entries if e.kind == "symlink"),
        total_bytes=state.total,
        ignored=state.ignored,
    )
    compressor = zstandard.ZstdCompressor(
        level=ZSTD_LEVEL, write_checksum=True, write_content_size=False, threads=0
    )
    with (
        compressor.stream_writer(cast("IO[bytes]", writer), closefd=False) as zstd_out,
        tarfile.open(
            fileobj=cast("IO[bytes]", zstd_out),
            mode="w|",
            format=tarfile.PAX_FORMAT,
            encoding="utf-8",
        ) as tar,
    ):
        for entry in state.entries:
            info = _tarinfo(entry)
            if entry.kind != "file":
                tar.addfile(info)
                continue
            with _open_regular(entry.path, entry.rel, entry.ident) as fh:
                try:
                    tar.addfile(info, fh)
                except (OSError, tarfile.TarError):
                    raise BundleError(
                        "file changed or became unreadable while packing",
                        path=entry.rel,
                    ) from None
                if fh.read(1):
                    raise BundleError(
                        "file grew while packing; pack again", path=entry.rel
                    )
    return PackResult(
        digest=Digest(writer.hasher.hexdigest()),
        compressed_bytes=writer.count,
        stats=stats,
    )


def pack_to_file(
    root: Path, dest: Path, *, limits: BundleLimits | None = None
) -> PackResult:
    """:func:`pack` into ``dest`` atomically (temp file in the same folder + rename).

    ``dest`` may live inside ``root``: it (and the temp file) are excluded.
    """
    root_real = Path(root).resolve()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    exclude: set[str] = set()
    for candidate in (dest, tmp):
        full = candidate.parent.resolve() / candidate.name
        if full.is_relative_to(root_real):
            exclude.add(full.relative_to(root_real).as_posix())
    try:
        with tmp.open("xb") as fh:
            result = pack(root, fh, limits=limits, exclude=exclude)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return result


# ========================================================================= unpack


def check_member_name(name: str, *, windows: bool = _WINDOWS) -> tuple[str, ...]:
    """Validate an archive member name; return its components. Raises BundleError.

    ``windows=True`` adds the rules that matter when extracting on Windows (drive
    letters and ``:`` streams, reserved device names, trailing dots/spaces).
    """
    if not name or "\x00" in name:
        raise BundleError("empty member name or NUL byte in a member name")
    if "\\" in name:
        raise BundleError("member name contains '\\'", path=name)
    if name.startswith("/") or _DRIVE_RE.match(name):
        raise BundleError("absolute member path", path=name)
    if len(name.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        raise BundleError(f"member path longer than {MAX_PATH_BYTES} bytes", path=name)
    parts = tuple(name.removesuffix("/").split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise BundleError("member path has an empty, '.' or '..' part", path=name)
        if len(part.encode("utf-8", "surrogatepass")) > MAX_COMPONENT_BYTES:
            raise BundleError("member name part too long", path=name)
        if windows and (
            any(ch in _WINDOWS_BAD_CHARS or ord(ch) < 32 for ch in part)
            or part.rstrip(" .") != part
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED
        ):
            raise BundleError("member name is not valid on Windows", path=name)
    return parts


def _check_link(link: str, rel: str) -> None:
    if not link or "\x00" in link or "\\" in link:
        raise BundleError("symlink target is empty or malformed", path=rel)
    if len(link.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        raise BundleError("symlink target too long", path=rel)
    if link.startswith("/") or _DRIVE_RE.match(link):
        raise BundleError("symlink target is absolute", path=rel)
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link))
    if joined == ".." or joined.startswith("../"):
        raise BundleError("symlink escapes the destination", path=rel)


class _GuardedTarInfo(tarfile.TarInfo):
    """Refuses oversized PAX / GNU long-name headers *before* tarfile reads them."""

    def _proc_member(self, tarfile_: tarfile.TarFile) -> Any:
        if self.type in _META_TYPES and self.size > MAX_META_BYTES:
            raise BundleError(
                f"archive metadata header larger than {MAX_META_BYTES} bytes"
            )
        return super()._proc_member(tarfile_)  # type: ignore[misc]


class _BoundedReader:
    """Read-only stream that raises once more than ``bound`` bytes were produced."""

    def __init__(self, raw: Any, bound: int) -> None:
        self._raw = raw
        self._bound = bound
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = _COPY_CHUNK
        data: bytes = self._raw.read(size)
        self.count += len(data)
        if self.count > self._bound:
            raise BundleError(
                f"decompressed archive larger than {self._bound} bytes "
                "(decompression bomb?)"
            )
        return data


@dataclass(slots=True)
class _Extraction:
    dest: Path
    limits: BundleLimits
    files: set[str] = field(default_factory=set)
    dirs: set[str] = field(default_factory=set)
    links: dict[str, str] = field(default_factory=dict)
    total: int = 0

    def count(self) -> int:
        return len(self.files) + len(self.dirs) + len(self.links)


def _ensure_parents(state: _Extraction, parts: tuple[str, ...], rel: str) -> None:
    for depth in range(1, len(parts)):
        ancestor = "/".join(parts[:depth])
        if ancestor in state.links or ancestor in state.files:
            raise BundleError("member is placed under a symlink or a file", path=rel)
        if ancestor in state.dirs:
            continue
        _mkdir(state, ancestor)


def _mkdir(state: _Extraction, rel: str) -> None:
    path = state.dest.joinpath(*rel.split("/"))
    try:
        path.mkdir(mode=0o755)
    except FileExistsError:
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode):  # pragma: no cover - tracked sets prevent it
            raise BundleError("folder collides with another member", path=rel) from None
    state.dirs.add(rel)
    if state.count() > state.limits.max_files:
        raise BundleError(f"archive has more than {state.limits.max_files} entries")


def _write_file(
    state: _Extraction, rel: str, member: tarfile.TarInfo, source: IO[bytes]
) -> None:
    mode = 0o755 if member.mode & stat.S_IXUSR else 0o644
    path = state.dest.joinpath(*rel.split("/"))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, mode)
    written = 0
    with os.fdopen(fd, "wb") as out:
        if hasattr(os, "fchmod"):
            os.fchmod(out.fileno(), mode)  # umask-independent
        while chunk := source.read(_COPY_CHUNK):
            written += len(chunk)
            if written > member.size:
                raise BundleError("member data longer than declared", path=rel)
            out.write(chunk)
    if written != member.size:
        raise BundleError("member data shorter than declared (truncated)", path=rel)
    state.files.add(rel)


def _extract_member(
    state: _Extraction, tar: tarfile.TarFile, member: tarfile.TarInfo
) -> None:
    parts = check_member_name(member.name)
    rel = "/".join(parts)
    if (
        rel in state.files
        or rel in state.links
        or (rel in state.dirs and not member.isdir())
    ):
        raise BundleError("duplicate member", path=rel)
    if member.type == tarfile.LNKTYPE:
        raise BundleError("hard links are not allowed", path=rel)
    if member.type not in (
        tarfile.REGTYPE,
        tarfile.AREGTYPE,
        tarfile.DIRTYPE,
        tarfile.SYMTYPE,
    ):
        raise BundleError(
            "only files, folders and symlinks are allowed (device, FIFO or sparse "
            "entry refused)",
            path=rel,
        )
    if state.count() + 1 > state.limits.max_files:
        raise BundleError(f"archive has more than {state.limits.max_files} entries")
    _ensure_parents(state, parts, rel)
    if member.isdir():
        if rel not in state.dirs:
            _mkdir(state, rel)
        return
    if member.issym():
        _check_link(member.linkname, rel)
        try:
            os.symlink(member.linkname, state.dest.joinpath(*parts))
        except OSError as exc:
            raise BundleError(
                "cannot create a symlink here "
                f"({exc.strerror or exc}; Windows needs Developer Mode)",
                path=rel,
                failure_type=FailureType.ENV_ERROR,
            ) from None
        state.links[rel] = member.linkname
        return
    state.total += member.size
    if state.total > state.limits.max_total_bytes:
        raise BundleError(
            f"archive content larger than {state.limits.max_total_bytes} bytes"
        )
    source = tar.extractfile(member)
    if source is None:  # pragma: no cover - regular members always have data
        raise BundleError("member has no data", path=rel)
    _write_file(state, rel, member, source)


def _verify_links(state: _Extraction) -> None:
    root_real = os.path.realpath(state.dest)
    for rel in state.links:
        real = os.path.realpath(state.dest.joinpath(*rel.split("/")))
        if os.path.commonpath([real, root_real]) != root_real:
            raise BundleError("symlink resolves outside the destination", path=rel)


def _prepare_dest(dest: Path) -> bool:
    """Create ``dest`` (True) or accept an existing *empty* folder (False)."""
    try:
        dest.mkdir(parents=True)
    except FileExistsError:
        if dest.is_symlink() or not dest.is_dir() or any(dest.iterdir()):
            raise BundleError(
                "unpack destination must be a new or empty folder"
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


def unpack(
    archive: Path | IO[bytes], dest: Path, *, limits: BundleLimits | None = None
) -> BundleStats:
    """Extract a bundle into ``dest`` (new or empty). See the module docstring.

    Raises :class:`BundleError` (``invalid_input``; ``disk_full`` when the disk
    ran out; ``env_error`` when this OS cannot create a symlink). On failure the
    destination is left empty (or removed, if this call created it).
    """
    limits = limits or BundleLimits()
    dest = Path(dest)
    created = _prepare_dest(dest)
    state = _Extraction(dest=dest, limits=limits)
    try:
        with contextlib.ExitStack() as stack:
            raw: IO[bytes] = (
                stack.enter_context(Path(archive).open("rb"))
                if isinstance(archive, (str, os.PathLike))
                else archive
            )
            decompressor = zstandard.ZstdDecompressor(max_window_size=MAX_WINDOW_SIZE)
            reader = stack.enter_context(decompressor.stream_reader(raw, closefd=False))
            bounded = _BoundedReader(reader, limits.stream_bound)
            tar = stack.enter_context(
                tarfile.open(
                    fileobj=cast("IO[bytes]", bounded),
                    mode="r|",
                    tarinfo=_GuardedTarInfo,
                    encoding="utf-8",
                    errors="strict",
                )
            )
            for member in tar:
                _extract_member(state, tar, member)
        _verify_links(state)
    except BundleError:
        _clean(dest, created)
        raise
    except OSError as exc:
        _clean(dest, created)
        if exc.errno == errno.ENOSPC:
            raise BundleError(
                "no space left while unpacking", failure_type=FailureType.DISK_FULL
            ) from None
        raise BundleError(
            f"cannot unpack ({type(exc).__name__}: {exc.strerror or 'I/O error'})"
        ) from None
    except (tarfile.TarError, zstandard.ZstdError, EOFError, UnicodeError) as exc:
        _clean(dest, created)
        raise BundleError(
            f"archive is corrupt or not a MeshForge bundle ({type(exc).__name__})"
        ) from None
    return BundleStats(
        files=len(state.files),
        dirs=len(state.dirs),
        symlinks=len(state.links),
        total_bytes=state.total,
    )
