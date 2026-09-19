"""Crash-safe file writes: the target is either the old file or the full new one.

The text goes to a temporary file in the target's directory, is flushed and
fsync'ed, and then ``os.replace`` swaps it in (atomic on Linux and Windows
within one file system). If anything fails or is interrupted (Ctrl-C) before
the swap, the temporary file is removed and the target is left untouched.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``text`` (LF newlines, parents created).

    Raises ``OSError``; on any exception the target is unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
