"""Advisory file lock (stdlib only): ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows.

One small OS split for callers that must serialise *processes* on a directory (the
node identity, ADR-0013). The lock file is created if missing and **never deleted**:
unlinking a lock another process is blocked on lets it lock a dead inode while a
third process creates a fresh file - two owners. The wait is bounded and polled
(non-blocking attempts) because a blocking lock has no portable timeout.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

__all__ = ["locked", "try_lock", "unlock"]


@contextlib.contextmanager
def locked(
    path: Path, *, timeout_s: float = 10.0, poll_s: float = 0.01, mode: int = 0o600
) -> Iterator[bool]:
    """Hold an exclusive advisory lock on ``path`` for the ``with`` block.

    Yields ``True`` when this call created the lock file (caller applies platform
    protection once). Raises ``TimeoutError`` after ``timeout_s`` if held elsewhere,
    ``OSError`` if the file cannot be opened. Opened with the builtin ``open`` and,
    when newly created, tightened to ``mode`` on POSIX (it holds no data).
    """
    created = not path.exists()
    fh: IO[bytes] = open(path, "a+b")  # noqa: SIM115 - closed in ``finally`` below
    try:
        if created and os.name == "posix":
            os.fchmod(fh.fileno(), mode)
        _acquire(fh, timeout_s, poll_s)
        try:
            yield created
        finally:
            _release(fh)
    finally:
        fh.close()


def _acquire(fh: IO[bytes], timeout_s: float, poll_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            _try_lock(fh)
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"lock not acquired within {timeout_s:g}s") from None
            time.sleep(poll_s)
        else:
            return


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # one byte at offset 0

    def _release(fh: IO[bytes]) -> None:
        fh.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def try_lock(fh: IO[bytes]) -> bool:
    """One non-blocking attempt at the exclusive lock on an open file (for
    holders that keep it for a process's lifetime, e.g. GC owners). False = held
    elsewhere (another process or another open of the file)."""
    try:
        _try_lock(fh)
    except OSError:
        return False
    return True


def unlock(fh: IO[bytes]) -> None:
    """Release a lock taken by :func:`try_lock`."""
    _release(fh)
