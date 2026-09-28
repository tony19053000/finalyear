"""Owner-only files: the one place that knows how to make a secret file private.

The same rule as the node identity (ADR-0013 §5), reusable by any process that
keeps a secret on disk (the controller's ``fabric.token``, D44) without importing
``meshforge.agent``:

* **POSIX** - the file is *created* ``0600`` (``os.open`` mode / :mod:`.atomic`), so
  there is nothing to do afterwards; :func:`private_problem` refuses a file whose
  group/other bits are set.
* **Windows** - no mode bits; ``icacls`` removes inherited ACEs and grants only the
  current user's SID (looked up with ``whoami /user /fo csv``, locale-independent).

Every failure is a :class:`PrivateFileError` (``config_error``) naming the path and
the remedy - never the file's content.
"""

from __future__ import annotations

import csv
import os
import stat
import subprocess
import sys
from pathlib import Path

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = ["PrivateFileError", "private_problem", "protect_private"]


class PrivateFileError(MeshForgeError):
    """A secret file could not be made (or is not) private to its owner."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


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
