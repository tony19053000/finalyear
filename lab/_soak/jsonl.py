"""Append-only JSON-lines files for the long runs (samples, ledger, action log).

Why not the ``_fabric.checkpoint`` JSON: a 72 h run sampled every minute holds
thousands of records; rewriting the whole file each minute is slow and a bigger
crash window. Here every record is **one line, appended and fsynced** at once,
so a power cut loses at most the line being written. On read, a torn last line
(no newline, or not JSON) is skipped and counted; a bad line *in the middle*
means the file was edited or corrupted and raises :class:`JsonlError`, so a
damaged ledger can never pass silently.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class JsonlError(ValueError):
    """A record in the middle of the file is not a JSON object."""


@dataclass(frozen=True, slots=True)
class Loaded:
    records: list[dict[str, Any]]
    torn_tail: bool
    """The last line was incomplete (a crash mid-write); it was skipped."""


def append(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, allow_nan=False, separators=(",", ":"))
    data = (line + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        _repair_tail(fd, path)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _repair_tail(fd: int, path: Path) -> None:
    """A torn last line (crash mid-write) gets its newline before the next
    record, so the torn fragment stays one bad line instead of merging with
    the new record."""
    size = os.fstat(fd).st_size
    if size == 0:
        return
    with path.open("rb") as fh:
        fh.seek(size - 1)
        if fh.read(1) != b"\n":
            os.write(fd, b"\n")


def load(path: Path) -> Loaded:
    if not path.exists():
        return Loaded([], False)
    raw = path.read_bytes().decode("utf-8", errors="replace")
    lines = raw.split("\n")
    complete, tail = lines[:-1], lines[-1]
    records: list[dict[str, Any]] = []
    torn = bool(tail.strip())
    for number, line in enumerate(complete, start=1):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            # A fragment left by a crash, later followed by good records
            # (append() closes it with a newline): skip it, but only if it is
            # not valid JSON at all - a valid non-object line is corruption.
            if doc is None and _looks_torn(line):
                torn = True
                continue
            raise JsonlError(f"{path.name}:{number}: not a JSON object")
        records.append(doc)
    return Loaded(records, torn)


def _looks_torn(line: str) -> bool:
    """A prefix of a record we wrote: starts like an object, never closed."""
    text = line.strip()
    return text.startswith("{") and not text.endswith("}")
