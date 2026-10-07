"""Owner-only files: the one place that knows how to make a secret file private.

The same rule as the node identity (ADR-0013 §5), reusable by any process that
keeps a secret on disk (the controller's ``fabric.token``, D44) without importing
``meshforge.agent``:

* **POSIX** - the file is *created* ``0600`` (``os.open`` mode / :mod:`.atomic`), so
  there is nothing to do afterwards; :func:`private_problem` refuses a file whose
  group/other bits are set.
* **Windows** - no mode bits; ``icacls`` removes inherited ACEs and grants only the
  current user's SID (looked up with ``whoami /user /fo csv``, locale-independent).

**Reading a secret file** (V0.7 D149a - one implementation for every secret file:
``ca.key``, ``server.key``, ``peer_signing.key``, ``fabric.token``, the agent's
``node.crt``/``ca.crt``): :func:`read_bounded` opens with ``O_NOFOLLOW |
O_NONBLOCK`` (a symlink is refused, a FIFO cannot block the open), ``fstat``s the
*opened* descriptor and refuses anything that is not a regular file (FIFO, device,
socket, folder), then reads at most ``limit`` bytes. :func:`read_private` adds the
owner check of :func:`stat_problem` (POSIX: no group/other bits, owned by the
effective user). Windows has neither ``O_NOFOLLOW`` nor FIFOs; its owner-only ACL
is applied at write time and not re-read (THREAT_MODEL LF-S2, accepted).

Every failure is a :class:`PrivateFileError` (``config_error``) naming the path and
the remedy - never the file's content. ``reason`` says which check failed.
"""

from __future__ import annotations

import csv
import os
import stat
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = [
    "PrivateFileError",
    "PrivateFileReason",
    "private_problem",
    "protect_private",
    "read_bounded",
    "read_private",
    "stat_problem",
]


class PrivateFileReason(StrEnum):
    """Which check a secret file failed (:attr:`PrivateFileError.reason`)."""

    MISSING = "missing"
    UNREADABLE = "unreadable"
    NOT_REGULAR = "not_regular"
    """A symlink, FIFO, device, socket or folder where a file belongs."""
    OVERSIZED = "oversized"
    NOT_PRIVATE = "not_private"
    """Group/other permission bits set, or owned by another user (POSIX)."""
    PROTECT_FAILED = "protect_failed"


class PrivateFileError(MeshForgeError):
    """A secret file could not be made (or is not) private to its owner."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object] | None = None,
        reason: PrivateFileReason = PrivateFileReason.PROTECT_FAILED,
    ) -> None:
        super().__init__(message, details=details)
        self.reason = reason


def protect_private(path: Path) -> None:
    """Restrict ``path`` to the current user (Windows ACL; no-op on POSIX)."""
    if sys.platform.startswith("win"):
        _protect_windows(path)


def private_problem(path: Path) -> str | None:
    """Why ``path`` is not private (POSIX mode bits), or ``None`` when it is.

    Windows ACLs are applied at write time by :func:`protect_private` and not
    re-read here (reading them back needs a second tool and parsing localised
    output); POSIX is checked on every load.
    """
    if os.name != "posix":
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"accessible to other users (mode {mode:04o}); chmod 600 it"
    return None


def stat_problem(info: os.stat_result) -> str | None:
    """Why an *opened* secret file is not private (POSIX: mode and owner).

    ``None`` on Windows (ACLs are applied at write time, see the module doc).
    """
    if os.name != "posix":
        return None
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return f"accessible to other users (mode {mode:04o}); chmod 600 it"
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return (
            f"owned by another user (uid {info.st_uid}); it must belong to the "
            "user running MeshForge (chown it)"
        )
    return None


def read_bounded(
    path: Path, *, limit: int, what: str, follow_symlinks: bool = False
) -> tuple[bytes, os.stat_result]:
    """Read at most ``limit`` bytes of a **regular file** at ``path``.

    A symlink is refused unless ``follow_symlinks`` (only for a *copy* a user
    points a tool at, e.g. ``compute --token-file``; never for a file MeshForge
    owns). Returns the bytes and the ``fstat`` of exactly the descriptor that was
    read, so callers check mode/owner of what they read (no check-then-open race).
    """
    details: dict[str, object] = {"path": str(path)}
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | (0 if follow_symlinks else getattr(os, "O_NOFOLLOW", 0))
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise PrivateFileError(
            f"the {what} {path} does not exist",
            details=details,
            reason=PrivateFileReason.MISSING,
        ) from None
    except OSError as exc:
        symlink = not follow_symlinks and path.is_symlink()
        raise PrivateFileError(
            f"cannot read the {what} {path}: {exc.strerror or exc}"
            + ("; it must be a regular file, not a symlink" if symlink else ""),
            details=details,
            reason=(
                PrivateFileReason.NOT_REGULAR
                if symlink
                else PrivateFileReason.UNREADABLE
            ),
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PrivateFileError(
                f"the {what} {path} is not a regular file",
                details=details,
                reason=PrivateFileReason.NOT_REGULAR,
            )
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        raise PrivateFileError(
            f"cannot read the {what} {path}: {exc.strerror or exc}",
            details=details,
            reason=PrivateFileReason.UNREADABLE,
        ) from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise PrivateFileError(
            f"the {what} {path} is larger than {limit} bytes",
            details=details,
            reason=PrivateFileReason.OVERSIZED,
        )
    return data, info


def read_private(path: Path, *, limit: int, what: str) -> bytes:
    """:func:`read_bounded` + :func:`stat_problem`: a secret file only its owner
    can read (POSIX), refused otherwise with ``reason=not_private``."""
    data, info = read_bounded(path, limit=limit, what=what)
    problem = stat_problem(info)
    if problem is not None:
        raise PrivateFileError(
            f"the {what} {path} is {problem}",
            details={"path": str(path)},
            reason=PrivateFileReason.NOT_PRIVATE,
        )
    return data


def _protect_windows(path: Path) -> None:  # pragma: no cover - Windows only
    sid = _current_user_sid()
    argv = ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv: a path and a SID
            argv, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise PrivateFileError(
            f"icacls could not be run for {path}: {exc}",
            details={"path": str(path)},
        ) from exc
    if result.returncode != 0:
        raise PrivateFileError(
            f"icacls failed for {path} (exit {result.returncode}); the file must "
            "be private to the current user - check its ownership",
            details={"path": str(path)},
        )


def _current_user_sid() -> str:  # pragma: no cover - Windows only
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv"],  # noqa: S607 - Windows built-in
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PrivateFileError(f"whoami could not be run: {exc}") from exc
    rows = list(csv.reader(result.stdout.splitlines()))
    if result.returncode != 0 or len(rows) < 2 or len(rows[1]) < 2:
        raise PrivateFileError(
            f"could not determine the current user's SID (exit {result.returncode})"
        )
    sid = rows[1][1].strip()
    if not sid.startswith("S-1-"):
        raise PrivateFileError("whoami returned an unexpected SID")
    return sid
