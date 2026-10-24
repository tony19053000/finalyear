"""Read (and rename) a user's identity / config **as root** without trusting the path.

``migrate-identity`` runs as root from ``postinst`` and reads folders a user owns
(``~/.local/share/meshforge/identity``, ``~/.config/meshforge``). A user who can
put a symlink there (``node.key -> /etc/shadow``, ``identity -> /root/...``) or
swap a folder between a check and a use must not make root read, copy or rename
anything that user could not reach themselves. So, for every source:

* the path is walked from ``/`` one component at a time with
  ``openat(O_NOFOLLOW | O_DIRECTORY)``: no symlink anywhere, each folder owned by
  root or the candidate user, none writable by anyone else (a root-owned sticky
  folder such as ``/tmp`` is fine; a group-writable one only when the group is the
  user's *private* group - Debian/Ubuntu "user private groups", where umask 002
  makes every folder ``0775``; the same exception Debian's OpenSSH makes);
* the last folder (the identity folder) and every file in it are owned by the
  candidate user, files are regular and opened ``O_NOFOLLOW`` through the held
  folder fd, and checked with ``fstat`` on the opened fd - what is checked is what
  is read;
* renames (``identity -> identity.migrated-<date>``) happen inside the held
  parent fd and are verified against the inode that was read; a swapped entry is
  put back and reported.

Any violation is an :class:`UnsafeSourceError`; the planner turns it into a
``blocked`` migration with the reason. POSIX only (the migration is Linux-only).
"""

from __future__ import annotations

import errno
import os
import stat
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.filelock import try_lock, unlock

_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS: Final = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
MAX_FILE_BYTES: Final = 1 << 20
"""Identity files and a config are a few KiB; anything bigger is not ours."""
LOCK_FILE_NAME: Final = "identity.lock"


class UnsafeSourceError(Exception):
    """A source root must not read: names the path and why."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"refusing to read {path} as root: {reason}")
        self.path = path
        self.reason = reason


@dataclass(slots=True)
class OpenDir:
    """A checked folder held open, and its (held) parent."""

    path: Path
    fd: int
    parent_fd: int
    uid: int

    def close(self) -> None:
        for fd in (self.fd, self.parent_fd):
            if fd >= 0:
                os.close(fd)
        self.fd = self.parent_fd = -1


@dataclass(frozen=True, slots=True)
class ReadFile:
    data: bytes
    mode: int
    dev: int
    ino: int


def _fstat(fd: int) -> os.stat_result:
    """Indirection so tests can present a foreign owner (no root in unit tests)."""
    return os.fstat(fd)


def _is_private_group(gid: int, uid: int) -> bool:
    """``gid`` is ``uid``'s own group: its primary group, no other member, and no
    other account has it as primary group."""
    import grp
    import pwd

    try:
        user = pwd.getpwuid(uid)
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    if user.pw_gid != gid or any(m != user.pw_name for m in group.gr_mem):
        return False
    return not any(p.pw_gid == gid and p.pw_uid != uid for p in pwd.getpwall())


def _writable_by_others(st: os.stat_result, uid: int, *, ancestor: bool) -> str | None:
    mode = stat.S_IMODE(st.st_mode)
    if mode & stat.S_IWOTH and not (
        ancestor and st.st_uid == 0 and mode & stat.S_ISVTX
    ):
        return f"it is writable by every user (mode {mode:04o})"
    if mode & stat.S_IWGRP and not (
        (ancestor and st.st_uid == 0 and st.st_gid == 0)
        or _is_private_group(st.st_gid, uid)
    ):
        return (
            f"it is writable by group {st.st_gid}, which other users are in "
            f"(mode {mode:04o})"
        )
    return None


def expected_uid(path: Path, home_root: Path) -> int:
    """Whose files ``path`` must be: the owner of the home folder it is in (below
    ``home_root``), else the owner of ``path`` itself. ``lstat`` only."""
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(home_root))
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = None
    probe = root / rel.parts[0] if rel is not None and rel.parts else path
    try:
        return os.lstat(probe).st_uid
    except OSError as exc:
        raise UnsafeSourceError(
            path, f"cannot inspect {probe}: {exc.strerror or exc}"
        ) from None


def _check_dir(fd: int, path: Path, uid: int, *, final: bool) -> None:
    st = _fstat(fd)
    if final and st.st_uid != uid:
        raise UnsafeSourceError(
            path, f"the folder belongs to uid {st.st_uid}, not to its user (uid {uid})"
        )
    if st.st_uid not in (0, uid):
        raise UnsafeSourceError(
            path, f"{path} belongs to uid {st.st_uid}, neither root nor uid {uid}"
        )
    problem = _writable_by_others(st, uid, ancestor=not final)
    if problem is not None:
        raise UnsafeSourceError(path, f"{path}: {problem}")


def _open_error(
    path: Path, parent_fd: int, name: str, exc: OSError
) -> UnsafeSourceError:
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
        try:
            if stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode):
                return UnsafeSourceError(path, f"{path} is a symbolic link")
        except OSError:
            pass
        return UnsafeSourceError(path, f"{path} is not a plain folder or file")
    if exc.errno == errno.ENOENT:
        return UnsafeSourceError(path, f"{path} does not exist")
    return UnsafeSourceError(path, f"could not read {path}: {exc.strerror or exc}")


def open_dir(path: Path, uid: int) -> OpenDir:
    """Walk ``path`` from ``/`` without following links (module docstring)."""
    path = Path(os.path.abspath(path))
    if len(path.parts) < 2:
        raise UnsafeSourceError(path, "not a user folder")
    fd = os.open("/", _DIR_FLAGS)
    parent = -1
    current = Path("/")
    try:
        _check_dir(fd, current, uid, final=False)
        last = len(path.parts) - 2
        for i, part in enumerate(path.parts[1:]):
            current = current / part
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise _open_error(current, fd, part, exc) from None
            if parent >= 0:
                os.close(parent)
            parent, fd = fd, child
            _check_dir(fd, current, uid, final=i == last)
    except BaseException:
        os.close(fd)
        if parent >= 0:
            os.close(parent)
        raise
    return OpenDir(path, fd, parent, uid)


@contextmanager
def opened_dir(path: Path, uid: int) -> Iterator[OpenDir]:
    held = open_dir(path, uid)
    try:
        yield held
    finally:
        held.close()


def read_file(folder: OpenDir, name: str) -> ReadFile | None:
    """``name`` in ``folder``, checked on its opened fd; ``None`` if absent."""
    path = folder.path / name
    try:
        fd = os.open(
            name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=folder.fd
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _open_error(path, folder.fd, name, exc) from None
    try:
        st = _fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeSourceError(path, f"{path} is not a regular file")
        if st.st_uid != folder.uid:
            raise UnsafeSourceError(
                path,
                f"{path} belongs to uid {st.st_uid}, not to the folder's user "
                f"(uid {folder.uid})",
            )
        problem = _writable_by_others(st, folder.uid, ancestor=False)
        if problem is not None:
            raise UnsafeSourceError(path, f"{path}: {problem}")
        if st.st_size > MAX_FILE_BYTES:
            raise UnsafeSourceError(path, f"{path} is too large ({st.st_size} bytes)")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise UnsafeSourceError(path, f"{path} is too large")
            chunks.append(chunk)
    except OSError as exc:
        raise UnsafeSourceError(
            path, f"could not read {path}: {exc.strerror or exc}"
        ) from None
    finally:
        os.close(fd)
    return ReadFile(b"".join(chunks), stat.S_IMODE(st.st_mode), st.st_dev, st.st_ino)


def read_files(folder: OpenDir, names: Iterable[str]) -> dict[str, ReadFile]:
    """The present ones of ``names`` (same checks as :func:`read_file`)."""
    out: dict[str, ReadFile] = {}
    for name in names:
        found = read_file(folder, name)
        if found is not None:
            out[name] = found
    return out


def has_entry(folder: OpenDir, name: str) -> bool:
    try:
        os.stat(name, dir_fd=folder.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


@contextmanager
def locked(
    folder: OpenDir, *, timeout_s: float, poll_s: float = 0.01
) -> Iterator[None]:
    """Hold the folder's ``identity.lock`` (as the agent does), opened through the
    folder fd without following links; a lock root creates is handed to the user."""
    path = folder.path / LOCK_FILE_NAME
    flags = os.O_RDWR | _O_NOFOLLOW | _O_CLOEXEC
    try:
        try:
            fd = os.open(
                LOCK_FILE_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=folder.fd
            )
            created = True
        except FileExistsError:
            fd = os.open(LOCK_FILE_NAME, flags, dir_fd=folder.fd)
            created = False
    except OSError as exc:
        raise _open_error(path, folder.fd, LOCK_FILE_NAME, exc) from None
    with os.fdopen(fd, "r+b", buffering=0) as fh:
        st = _fstat(fh.fileno())
        if created and st.st_uid != folder.uid:
            os.fchown(fh.fileno(), folder.uid, -1)
        elif not stat.S_ISREG(st.st_mode) or st.st_uid != folder.uid:
            raise UnsafeSourceError(
                path, f"{path} is not a plain file of uid {folder.uid}"
            )
        deadline = time.monotonic() + timeout_s
        while not try_lock(fh):
            if time.monotonic() >= deadline:
                raise UnsafeSourceError(
                    path,
                    "the identity is locked by another process (a running user "
                    "agent?); stop it and re-run",
                )
            time.sleep(poll_s)
        try:
            yield
        finally:
            unlock(fh)


def identity_of(fd: int) -> tuple[int, int]:
    """``(st_dev, st_ino)`` of an open fd (what :func:`rename_aside` expects)."""
    st = os.fstat(fd)
    return st.st_dev, st.st_ino


def rename_aside(
    parent_fd: int, parent: Path, name: str, today: str, expect: tuple[int, int]
) -> Path:
    """Rename ``name`` -> ``name.migrated-<today>[-N]`` inside the held
    ``parent_fd``; if the entry is not the inode that was read, undo and refuse."""
    base = f"{name}.migrated-{today}"
    candidate, n = base, 2
    while True:
        try:
            os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            break
        candidate, n = f"{base}-{n}", n + 1
    os.rename(name, candidate, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    moved = os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
    if (moved.st_dev, moved.st_ino) != expect:
        os.rename(candidate, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        raise UnsafeSourceError(
            parent / name,
            f"{parent / name} was replaced while it was being migrated; left as is",
        )
    return parent / candidate
