"""What one chunk's process sees (V0.10 G4, D260/D264/D278). Pure + small
blocking file writers (run them in a thread).

**The chunk folder.** Each chunk gets a fresh agent-owned control folder
outside the workdir (:mod:`.chunk_folder`) holding the items file and an empty
``out/`` folder; the project (staged once per lane) is the working directory,
exactly as for a whole-job run. The folder is removed after the chunk (its
``out/`` packed first).

**``{items_file}`` / ``MESHFORGE_CHUNK_ITEMS``** - the chunk's items, never in
argv or a shell (D278: an item is opaque data):

* ``glob``, ``list``, ``list_file``, ``range``: a UTF-8 **JSON array** of the
  chunk's items in item-list order (strings; integers for ``range``) - JSON
  escapes every newline, quote and control character, so no item can break the
  file's structure. For ``glob`` each item is the file's path relative to the
  working directory, and the file is there (read-only) while the chunk runs;
* ``lines``: the chunk's **records file** - the header line (``header: true``)
  then the chunk's records, byte for byte as in the source file (any encoding;
  nothing decoded or re-encoded), so ``csv.reader(open(items_file))`` reads the
  chunk like a small CSV.

**``{chunk_index}`` / ``MESHFORGE_CHUNK_INDEX``** - the chunk's number (0-based,
item-list order). **``{out}`` / ``MESHFORGE_CHUNK_OUT``** - the empty folder
whose files are the chunk's result (packed, uploaded and later combined in
chunk order). Paths are the ones the process sees: absolute host paths for the
subprocess / venv runtimes, ``/meshforge/in/...`` and ``/meshforge/out``
inside the Docker sandbox.

Placeholders are replaced **once**, left to right, inside any argv item
(``--in={items_file}`` works); a value is never scanned again, so a path that
happens to contain ``{out}`` cannot be expanded twice. Any other ``{...}`` text
is left alone.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO, Final

__all__ = [
    "chunk_env",
    "split_view_path",
    "substitute",
    "write_items_json",
    "write_lines_chunk",
]

_PLACEHOLDER: Final = re.compile(r"\{(items_file|chunk_index|out)\}")
_COPY: Final = 1024 * 1024


def substitute(argv: Sequence[str], values: Mapping[str, str]) -> tuple[str, ...]:
    """Replace ``{items_file}``, ``{chunk_index}``, ``{out}`` (module
    docstring); ``values`` maps each name (without braces) to its text."""
    return tuple(_PLACEHOLDER.sub(lambda m: values[m.group(1)], arg) for arg in argv)


def chunk_env(
    env: Mapping[str, str], *, items_file: str, chunk_index: int, out: str
) -> dict[str, str]:
    merged = dict(env)
    merged["MESHFORGE_CHUNK_ITEMS"] = items_file
    merged["MESHFORGE_CHUNK_INDEX"] = str(chunk_index)
    merged["MESHFORGE_CHUNK_OUT"] = out
    return merged


def _create(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags | getattr(os, "O_BINARY", 0), 0o644), "wb")


def write_items_json(path: Path, items: Sequence[str | int]) -> int:
    """The JSON array of the chunk's items (D278); returns its size."""
    data = (json.dumps(list(items), ensure_ascii=False) + "\n").encode("utf-8")
    with _create(path) as out:
        out.write(data)
    return len(data)


def write_lines_chunk(
    path: Path,
    source: BinaryIO,
    *,
    header: tuple[int, int] | None,
    records: Sequence[tuple[int, int]],
) -> int:
    """The header line then the chunk's records, copied byte for byte from
    ``source`` (the verified source blob); returns the bytes written."""
    written = 0
    with _create(path) as out:
        for offset, length in [*([header] if header else []), *records]:
            source.seek(offset)
            left = length
            while left > 0:
                data = source.read(min(_COPY, left))
                if not data:
                    raise ValueError("the source file is shorter than its records")
                out.write(data)
                left -= len(data)
                written += len(data)
    return written


def split_view_path(path: str, mounts: Sequence[str]) -> tuple[str | None, str]:
    """``(mount, path in that input)`` of an item path of the split view, or
    ``(None, path)`` for a project path (mounts never overlap project files)."""
    for mount in mounts:
        if path.startswith(mount + "/"):
            return mount, path[len(mount) + 1 :]
    return None, path
