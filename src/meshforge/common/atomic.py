"""Crash-safe file writes: the target is either the old file or the full new one.

The bytes go to a temporary file created **in the target's directory** with the
requested permission bits (``os.open`` with ``O_CREAT | O_EXCL``, so the file
never exists with looser permissions), are flushed and ``fsync``'ed, and then
``os.replace`` swaps the temporary file in - atomic on Linux and Windows within
one file system. If anything fails or is interrupted before the swap, the
temporary file is removed and the target is left byte-identical.

Used for state that must never be half-written: the node identity (ADR-0013 §5).
Deliberately independent of ``lab/`` (``src`` never imports ``lab``).
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

__all__ = ["atomic_write_bytes"]


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace ``path`` with ``data``.

    ``mode`` is the permission bits the temporary (and therefore the final) file
    is created with; ignored by the OS on Windows (ACLs are applied by the caller).
    The parent directory must already exist. Raises ``OSError`` on failure; on any
    exception the target is unchanged and no temporary file is left behind.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Persist the directory entry (POSIX only; Windows cannot open a dir for fsync)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some file systems refuse directory fsync; the rename itself succeeded
    finally:
        os.close(fd)
