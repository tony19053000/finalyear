"""Resolve a ``split:`` spec to its items, once, at submit (V0.10 G1, D260, D273).

Pure: no database, no network, no clock. The controller calls :func:`resolve`
on every split submission against the **uploaded** tree manifests (the project
and every named input); ``compute run --dry-run`` calls it against the tree it
just built, so both print the same items.

**The views.** Items live in one virtual tree: the project's entries at their
own paths plus every named input's entries under its mount (``data/x.jpg`` of
the input mounted at ``in`` is ``in/data/x.jpg``). Mounts never overlap project
files (checked at submit), so every path names one entry.

**Items per source** (``split.over``):

* ``glob`` - the **files** whose path matches (``*``/``?``/``[...]`` inside one
  path component, ``**`` for any number of folders - the rules of ``outputs``),
  **sorted by their UTF-8 bytes** (so ``B`` < ``a`` < ``é``), each path once. A
  matching symlink is followed inside its own tree (at most 40 hops) and must
  end at a file; a folder entry never matches. Each item carries the file's
  digest and size (what a chunk will stage, D264).
* ``list`` - the strings as written (the manifest refused repeats).
* ``list_file`` - one item per line of a UTF-8 file of the tree (BOM and ``\\r``
  stripped, blank lines skipped); order as written; a repeated item or a line
  over :data:`~meshforge.common.manifest.MAX_SPLIT_ITEM_CHARS` is refused with
  its line number.
* ``range`` - the integers; counted before any is built.
* ``lines`` - the **records** of a file of the tree: each line with its ``\\n``
  (a last line without one counts), blank lines skipped. An item is the record's
  index; :attr:`ResolvedItems.records` holds each record's ``(offset, length)``
  in the file and ``header`` the first line's when ``header: true``. Bytes are
  never decoded (any encoding works).

**Limits** (:class:`SplitLimits`): 1..``max_items`` items (0 items is
``invalid_input`` - a split job always has work), at most ``max_chunks``
chunks for a fixed ``chunk: {items: N}``, bounded reads of ``list_file`` /
``lines``. Scanning stops as soon as a cap is passed.

Every refusal is a :class:`~meshforge.common.manifest.ManifestError` naming the
YAML path (``split.over.glob: matched no files``) - ``invalid_input``.

**The item list blob** (:meth:`ResolvedItems.blob`) is canonical JSON (sorted
keys, no spaces, UTF-8), so the same items always give the same digest::

    {
        "count": 2,
        "items": ["a.jpg", "b.jpg"],
        "over": "glob",
        "v": 1,
        "files": [["<sha256 hex>", 1234], ["<sha256 hex>", 99]],
    }

plus ``source`` (``{"path", "sha256", "size"}`` of the ``list_file`` /
``lines`` file) and, for ``lines``, ``records`` (``[[offset, length], ...]``)
and ``header`` (``[offset, length]`` or null).
"""

from __future__ import annotations

import json
import math
import posixpath
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import IO, Any, Final

from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.manifest import (
    MAX_SPLIT_CHUNKS,
    MAX_SPLIT_ITEM_CHARS,
    MAX_SPLIT_ITEMS,
    ManifestError,
    OverKind,
    SplitSpec,
)
from meshforge.common.tree import Tree, TreeEntry

__all__ = [
    "ITEMS_BLOB_VERSION",
    "MAX_SYMLINK_HOPS",
    "ItemFile",
    "Opener",
    "ResolvedItems",
    "SplitLimits",
    "chunk_count",
    "glob_match",
    "glob_parts",
    "may_enter",
    "resolve",
]

ITEMS_BLOB_VERSION: Final = 1
MAX_SYMLINK_HOPS: Final = 40
_READ: Final = 1024 * 1024

type Opener = Callable[[str], IO[bytes]]
"""Opens a stored file by its SHA-256 hex (the controller's CAS, the CLI's
local file); the caller's file object is closed by :func:`resolve`."""


@dataclass(frozen=True, slots=True)
class SplitLimits:
    """Caps for one resolution (``[controller.split]``, D275)."""

    max_items: int = MAX_SPLIT_ITEMS
    max_chunks: int = MAX_SPLIT_CHUNKS
    max_list_file_bytes: int = 64 * 1024 * 1024
    """A ``list_file`` larger than this is refused (it is read whole)."""
    max_lines_file_bytes: int = 2 * 1024**3
    """A ``lines`` file larger than this is refused (it is streamed once)."""
    max_record_bytes: int = 16 * 1024 * 1024
    """A ``lines`` record (one line) longer than this is refused: the scan
    holds at most one record plus one read block in memory."""

    def __post_init__(self) -> None:
        if not 1 <= self.max_items <= MAX_SPLIT_ITEMS:
            raise ValueError(f"max_items must be 1..{MAX_SPLIT_ITEMS}")
        if not 1 <= self.max_chunks <= MAX_SPLIT_CHUNKS:
            raise ValueError(f"max_chunks must be 1..{MAX_SPLIT_CHUNKS}")
        if (
            min(
                self.max_list_file_bytes,
                self.max_lines_file_bytes,
                self.max_record_bytes,
            )
            < 1
        ):
            raise ValueError("file caps must be positive")


@dataclass(frozen=True, slots=True)
class ItemFile:
    """A stored file: an item of a ``glob``, or the file a ``list_file`` /
    ``lines`` source was read from (``path`` = its path in the view)."""

    path: str
    sha256: str
    size: int

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class ResolvedItems:
    """The items of one split job, in their one fixed order (checklist 33)."""

    over: OverKind
    items: tuple[str | int, ...]
    files: tuple[ItemFile, ...] = ()
    """``glob``: one per item, same order (``files[i].path == items[i]``)."""
    source: ItemFile | None = None
    """``list_file`` / ``lines``: the file read."""
    records: tuple[tuple[int, int], ...] = ()
    """``lines``: ``(offset, length)`` of each record, same order as items."""
    header: tuple[int, int] | None = None
    """``lines`` with ``header: true``: the first line's ``(offset, length)``."""
    _blob: list[bytes] = field(default_factory=list, repr=False, compare=False)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def item_bytes(self) -> int | None:
        """Bytes the items stand for: the files of a ``glob``, the records of
        ``lines``; None for strings and integers (no data of their own)."""
        if self.over == "glob":
            return sum(f.size for f in self.files)
        if self.over == "lines":
            return sum(length for _, length in self.records)
        return None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "v": ITEMS_BLOB_VERSION,
            "over": self.over,
            "count": self.count,
            "items": list(self.items),
        }
        if self.over == "glob":
            out["files"] = [[f.sha256, f.size] for f in self.files]
        if self.source is not None:
            out["source"] = self.source.to_json()
        if self.over == "lines":
            out["records"] = [[o, n] for o, n in self.records]
            out["header"] = None if self.header is None else list(self.header)
        return out

    def blob(self) -> bytes:
        """The canonical item-list bytes (see module docstring); computed once."""
        if not self._blob:
            self._blob.append(
                json.dumps(
                    self.to_json(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )
        return self._blob[0]

    @property
    def digest(self) -> Digest:
        return sha256_bytes(self.blob())


# ------------------------------------------------------------------- globbing


def glob_parts(pattern: str) -> tuple[str, ...]:
    """A validated pattern (``split.over.glob``) as its path components."""
    return tuple(pattern.split("/"))


def _advance(
    states: frozenset[int], parts: tuple[str, ...], name: str
) -> frozenset[int]:
    out: set[int] = set()
    for i in states:
        if i >= len(parts):
            continue
        if parts[i] == "**":
            out.add(i)
        elif fnmatchcase(name, parts[i]):
            out.add(i + 1)
    return _closure(frozenset(out), parts)


def _closure(states: frozenset[int], parts: tuple[str, ...]) -> frozenset[int]:
    """``**`` may also match zero folders: add the state after it."""
    out = set(states)
    stack = list(states)
    while stack:
        i = stack.pop()
        if i < len(parts) and parts[i] == "**" and i + 1 not in out:
            out.add(i + 1)
            stack.append(i + 1)
    return frozenset(out)


def glob_match(parts: tuple[str, ...], path: str) -> bool:
    """Does the path match the pattern components? A set-of-states walk:
    linear in ``len(parts) * len(path components)`` whatever the ``**``."""
    states = _closure(frozenset({0}), parts)
    for name in path.split("/"):
        states = _advance(states, parts, name)
        if not states:
            return False
    return len(parts) in states


def may_enter(parts: tuple[str, ...], folder: str) -> bool:
    """Could a path below ``folder`` match? (A named input's mount: skip its
    tree when no path under it can match.)"""
    states = _closure(frozenset({0}), parts)
    for name in folder.split("/"):
        states = _advance(states, parts, name)
        if not states:
            return False
    return True


# ---------------------------------------------------------------------- views


@dataclass(frozen=True, slots=True)
class _View:
    prefix: str
    """``""`` for the project, ``"<mount>/"`` for a named input."""
    by_path: Mapping[str, TreeEntry]


def _views(project: Tree, inputs: Mapping[str, Tree]) -> list[_View]:
    return [_View("", {e.path: e for e in project.entries})] + [
        _View(mount + "/", {e.path: e for e in inputs[mount].entries})
        for mount in sorted(inputs)
    ]


def _follow(view: _View, entry: TreeEntry, yaml_path: str, shown: str) -> TreeEntry:
    """The file a symlink leads to inside its own tree (parse_tree already
    refused targets that leave it; checked again here)."""
    hops = 0
    while entry.kind == "symlink":
        hops += 1
        if hops > MAX_SYMLINK_HOPS:
            raise ManifestError.one(yaml_path, f"{shown}: too many symlink hops")
        target = posixpath.normpath(
            posixpath.join(posixpath.dirname(entry.path), entry.link)
        )
        if target == ".." or target.startswith(("../", "/")):
            raise ManifestError.one(yaml_path, f"{shown}: symlink leaves its tree")
        nxt = view.by_path.get(target)
        if nxt is None:
            below = target + "/"
            if any(path.startswith(below) for path in view.by_path):
                # a folder with files is implied by them (no entry of its own)
                raise ManifestError.one(yaml_path, f"{shown}: is a folder, not a file")
            raise ManifestError.one(
                yaml_path, f"{shown}: symlink target is not in the tree"
            )
        entry = nxt
    if entry.kind != "file":
        raise ManifestError.one(yaml_path, f"{shown}: is a folder, not a file")
    return entry


def _find(views: Sequence[_View], path: str, yaml_path: str) -> tuple[_View, TreeEntry]:
    for view in reversed(views):  # inputs first: their prefixes are exact
        if view.prefix and not path.startswith(view.prefix):
            continue
        rel = path[len(view.prefix) :]
        entry = view.by_path.get(rel)
        if entry is None:
            if any(p.startswith(rel + "/") for p in view.by_path):
                raise ManifestError.one(yaml_path, "is a folder, not a file")
            break
        return view, _follow(view, entry, yaml_path, "the file")
    raise ManifestError.one(
        yaml_path, "no such file in the project or its named inputs"
    )


# -------------------------------------------------------------------- sources


def _too_many(yaml_path: str, limits: SplitLimits) -> ManifestError:
    return ManifestError.one(
        yaml_path,
        f"more than {limits.max_items} items (the controller's split.max_items)",
    )


def _glob(pattern: str, views: Sequence[_View], limits: SplitLimits) -> ResolvedItems:
    yaml_path = "split.over.glob"
    parts = glob_parts(pattern)
    found: dict[str, ItemFile] = {}
    for view in views:
        if view.prefix and not may_enter(parts, view.prefix.rstrip("/")):
            continue
        for rel, entry in view.by_path.items():
            if entry.kind == "dir":
                continue
            path = view.prefix + rel
            if not glob_match(parts, path):
                continue
            target = _follow(view, entry, yaml_path, "a matched path")
            found[path] = ItemFile(path, target.sha256, target.size)
            if len(found) > limits.max_items:
                raise _too_many(yaml_path, limits)
    if not found:
        raise ManifestError.one(yaml_path, "matched no files")
    order = sorted(found, key=lambda p: p.encode("utf-8"))
    return ResolvedItems(
        over="glob", items=tuple(order), files=tuple(found[p] for p in order)
    )


def _read_all(open_blob: Opener, entry: TreeEntry, cap: int) -> bytes:
    with open_blob(entry.sha256) as fh:
        data = fh.read(cap + 1)
    return data


def _list_file(
    path: str,
    views: Sequence[_View],
    open_blob: Opener,
    limits: SplitLimits,
) -> ResolvedItems:
    yaml_path = "split.over.list_file"
    _, entry = _find(views, path, yaml_path)
    if entry.size > limits.max_list_file_bytes:
        raise ManifestError.one(
            yaml_path, f"the file is larger than {limits.max_list_file_bytes} bytes"
        )
    raw = _read_all(open_blob, entry, limits.max_list_file_bytes)
    try:
        text = raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError.one(yaml_path, "the file is not UTF-8 text") from None
    items: list[str] = []
    seen: set[str] = set()
    for number, line in enumerate(text.split("\n"), start=1):
        item = line.removesuffix("\r")
        if not item.strip():
            continue
        if len(item) > MAX_SPLIT_ITEM_CHARS:
            raise ManifestError.one(
                yaml_path,
                f"line {number} is longer than {MAX_SPLIT_ITEM_CHARS} characters",
            )
        if "\x00" in item:
            raise ManifestError.one(yaml_path, f"line {number} contains a NUL byte")
        if item in seen:
            raise ManifestError.one(yaml_path, f"line {number} repeats an earlier item")
        seen.add(item)
        items.append(item)
        if len(items) > limits.max_items:
            raise _too_many(yaml_path, limits)
    if not items:
        raise ManifestError.one(yaml_path, "the file lists no items")
    return ResolvedItems(
        over="list_file",
        items=tuple(items),
        source=ItemFile(path, entry.sha256, entry.size),
    )


class _LongLineError(Exception):
    """A ``lines`` record over ``max_record_bytes`` (internal)."""


def _records(fh: IO[bytes], max_record: int) -> Iterator[tuple[int, int, bool]]:
    """``(offset, length, blank)`` of every line, its ``\\n`` included.
    Raises :class:`_LongLineError` for a line longer than ``max_record``."""
    offset = 0
    pending = b""
    while True:
        block = fh.read(_READ)
        if not block:
            break
        pending += block
        if len(pending) > max_record and pending.find(b"\n", 0, max_record) == -1:
            raise _LongLineError
        start = 0
        while (nl := pending.find(b"\n", start)) != -1:
            line = pending[start : nl + 1]
            if len(line) > max_record:
                raise _LongLineError
            yield offset, len(line), not line.strip()
            offset += len(line)
            start = nl + 1
        pending = pending[start:]
    if len(pending) > max_record:
        raise _LongLineError
    if pending:
        yield offset, len(pending), not pending.strip()


def _lines(
    path: str,
    header: bool,
    views: Sequence[_View],
    open_blob: Opener,
    limits: SplitLimits,
) -> ResolvedItems:
    yaml_path = "split.over.lines"
    _, entry = _find(views, path, yaml_path)
    if entry.size > limits.max_lines_file_bytes:
        raise ManifestError.one(
            yaml_path, f"the file is larger than {limits.max_lines_file_bytes} bytes"
        )
    records: list[tuple[int, int]] = []
    head: tuple[int, int] | None = None
    with open_blob(entry.sha256) as fh:
        scan = _records(fh, limits.max_record_bytes)
        try:
            for offset, length, blank in scan:
                if header and head is None:
                    head = (offset, length)
                    continue
                if blank:
                    continue
                records.append((offset, length))
                if len(records) > limits.max_items:
                    raise _too_many(yaml_path, limits)
        except _LongLineError:
            raise ManifestError.one(
                yaml_path,
                f"a line is longer than {limits.max_record_bytes} bytes",
            ) from None
    if not records:
        raise ManifestError.one(
            yaml_path,
            "the file has no records" + (" after its header line" if header else ""),
        )
    return ResolvedItems(
        over="lines",
        items=tuple(range(len(records))),
        source=ItemFile(path, entry.sha256, entry.size),
        records=tuple(records),
        header=head,
    )


# ----------------------------------------------------------------------- API


def chunk_count(items: int, per_chunk: int) -> int:
    return math.ceil(items / per_chunk)


def resolve(
    spec: SplitSpec,
    project: Tree,
    inputs: Mapping[str, Tree],
    open_blob: Opener,
    limits: SplitLimits | None = None,
) -> ResolvedItems:
    """The items of ``spec`` (module docstring). ``inputs`` maps each named
    input's mount to its tree; ``open_blob`` reads a file of either.

    Raises :class:`ManifestError` (``invalid_input``) naming the YAML path."""
    limits = limits or SplitLimits()
    views = _views(project, inputs)
    over = spec.over
    resolved: ResolvedItems
    if over.glob is not None:
        resolved = _glob(over.glob, views, limits)
    elif over.list is not None:
        if len(over.list) > limits.max_items:
            raise _too_many("split.over.list", limits)
        if not over.list:
            raise ManifestError.one("split.over.list", "the list is empty")
        resolved = ResolvedItems(over="list", items=tuple(over.list))
    elif over.list_file is not None:
        resolved = _list_file(over.list_file, views, open_blob, limits)
    elif over.range is not None:
        count = over.range.count
        if count == 0:
            raise ManifestError.one("split.over.range", "the range is empty")
        if count > limits.max_items:
            raise _too_many("split.over.range", limits)
        resolved = ResolvedItems(
            over="range",
            items=tuple(range(over.range.start, over.range.stop, over.range.step)),
        )
    else:
        assert over.lines is not None  # noqa: S101 - SplitOver validated one source
        resolved = _lines(over.lines, over.header, views, open_blob, limits)
    per_chunk = spec.chunk.items
    if per_chunk is not None:
        chunks = chunk_count(resolved.count, per_chunk)
        if chunks > limits.max_chunks:
            raise ManifestError.one(
                "split.chunk.items",
                f"{resolved.count} items in chunks of {per_chunk} make {chunks} "
                f"chunks, more than {limits.max_chunks} (the controller's "
                "split.max_chunks); use larger chunks or chunk: auto",
            )
    return resolved
