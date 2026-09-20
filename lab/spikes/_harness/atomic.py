"""Crash-safe file writes: the target is either the old file or the full new one.

The text goes to a temporary file in the target's directory, is flushed and
fsync'ed, and then ``os.replace`` swaps it in (atomic on Linux and Windows
within one file system). If anything fails or is interrupted (Ctrl-C) before
the swap, the temporary file is removed and the target is left untouched. Text
that cannot be encoded is refused before anything is created, as an
:class:`UnencodableTextError` (an ``OSError``), never as a bare
``UnicodeEncodeError`` from inside the write.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


class UnencodableTextError(OSError):
    """``text`` cannot be encoded in the target encoding, so nothing is written.

    Result files are data written by another process: JSON allows an unpaired
    UTF-16 surrogate escape (``"\\ud800"``), which ``json.loads`` decodes into a
    Python string that has no UTF-8 encoding. Such a value can reach a rendered
    report. Refusing with an ``OSError`` keeps the write contract ("refuse
    loudly, the target is byte-identical") instead of raising
    ``UnicodeEncodeError`` out of the middle of a write.
    """


def _unencodable(path: Path, text: str, exc: UnicodeEncodeError) -> str:
    """An ASCII-safe description of ``exc`` (the culprit cannot be printed)."""
    line = text.count("\n", 0, exc.start) + 1
    column = exc.start - (text.rfind("\n", 0, exc.start) + 1) + 1
    return (
        f"{path}: the text cannot be encoded as {exc.encoding}: {exc.reason} at "
        f"line {line}, column {column} ({text[exc.start : exc.end]!a}). "
        "Nothing was written, the file is unchanged. This usually means a "
        "result file carries a lone surrogate (a '\\udXXX' escape in its JSON); "
        "fix or remove that file and re-run."
    )


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``text`` (LF newlines, parents created).

    Raises ``OSError`` - :class:`UnencodableTextError` when ``text`` cannot be
    encoded; on any exception the target is unchanged.
    """
    try:  # before any file is touched, so an unencodable text writes nothing
        text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise UnencodableTextError(_unencodable(path, text, exc)) from exc
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
