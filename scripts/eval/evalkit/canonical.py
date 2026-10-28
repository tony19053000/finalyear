"""Canonical JSON, hashes and atomic writes (D419: byte-identical outputs).

Every number the analysis writes goes through :func:`num` (rounded to
:data:`DIGITS` decimals, ``-0.0`` -> ``0.0``), every document through
:func:`dumps` (sorted keys, two-space indent, LF, trailing newline, no NaN /
Infinity literal). The same input therefore gives the same bytes on every
machine with the same Python.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DIGITS",
    "dumps",
    "num",
    "sha256_bytes",
    "sha256_file",
    "write_atomic",
    "write_text",
]

DIGITS: Final = 6
"""Decimals every float of the analysis is rounded to."""


def num(value: float | None, digits: int = DIGITS) -> float | None:
    """``value`` rounded for output; None stays None; a non-finite value is a
    bug upstream (raises)."""
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"non-finite number in the analysis: {value!r}")
    out = round(float(value), digits)
    return 0.0 if out == 0 else out


def dumps(value: Any) -> str:
    """The canonical text of ``value``."""
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a sibling temp file + rename (a
    reader never sees half a file; a crash leaves the old one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_text(path: Path, text: str) -> None:
    """:func:`write_atomic` of UTF-8 text with LF line endings."""
    write_atomic(path, text.encode("utf-8"))
