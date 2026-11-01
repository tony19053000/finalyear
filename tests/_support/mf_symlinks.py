"""Symlink creation for tests that must run on hosts which refuse symlinks.

Windows lets a process create a symbolic link only with the
``SeCreateSymbolicLinkPrivilege`` (an elevated shell) or with Developer Mode on;
otherwise ``os.symlink`` fails with ``WinError 1314`` (``ERROR_PRIVILEGE_NOT_HELD``).
Some POSIX filesystems (FAT, certain network/FUSE mounts) refuse them too
(``EPERM`` / ``ENOTSUP``).

A test whose *setup* needs a link calls :func:`make_symlink` instead of
``Path.symlink_to`` / ``os.symlink``: the link is created where the OS allows it,
and the test is **skipped with a clear reason** where the OS refuses it. Any other
``OSError`` (a missing parent, a name clash) still fails the test - only the
"this host does not allow symlinks" answer becomes a skip. :func:`can_symlink`
probes the host once for tests that need to know up front.
"""

from __future__ import annotations

import errno
import functools
import os
import tempfile
from pathlib import Path
from typing import Final

import pytest

#: ``ERROR_PRIVILEGE_NOT_HELD``: no symlink privilege (Windows, Developer Mode off).
WINERROR_PRIVILEGE_NOT_HELD: Final = 1314

#: errno values a POSIX filesystem answers when it does not support symlinks.
REFUSED_ERRNOS: Final = frozenset(
    {errno.EPERM, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
)

SYMLINK_REFUSED_REASON: Final = (
    "this OS refuses to create symlinks here (Windows: enable Developer Mode or "
    "run elevated; WinError 1314 = no symlink privilege)"
)


def symlink_refused(exc: OSError) -> bool:
    """Whether ``exc`` (from ``os.symlink``) means "symlinks are not allowed here"."""
    if getattr(exc, "winerror", None) == WINERROR_PRIVILEGE_NOT_HELD:
        return True
    return os.name == "posix" and exc.errno in REFUSED_ERRNOS


def make_symlink(
    link: Path, target: Path | str, *, target_is_directory: bool = False
) -> None:
    """Create ``link`` -> ``target``, or skip the calling test if the OS refuses.

    ``NotImplementedError`` (a platform without ``os.symlink``) also skips.
    """
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except NotImplementedError:
        pytest.skip(SYMLINK_REFUSED_REASON)
    except OSError as exc:
        if symlink_refused(exc):
            pytest.skip(f"{SYMLINK_REFUSED_REASON}: {exc}")
        raise


@functools.cache
def can_symlink() -> bool:
    """Whether this process may create a symlink in the temp directory (cached)."""
    with tempfile.TemporaryDirectory(prefix="mf-symlink-probe-") as scratch:
        try:
            os.symlink(Path(scratch) / "target", Path(scratch) / "link")
        except NotImplementedError:
            return False
        except OSError as exc:
            if symlink_refused(exc):
                return False
            raise
    return True
