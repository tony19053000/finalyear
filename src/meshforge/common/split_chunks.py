"""Chunks of a split job: what both the controller and a lane compute the same
way (V0.10 G4, D259/D263/D264; pure, stdlib + common only).

**Chunks.** A split job's items (the item list resolved at submit,
:mod:`meshforge.common.split_items`, fixed order - checklist 33) are cut into
consecutive chunks of ``chunk_items`` items (the last may be shorter): chunk
``k`` holds items ``[k * chunk_items, min((k + 1) * chunk_items, count))``.
Each chunk is one work-queue item; its payload is ``{"first", "count"}``.

**Slice digest.** A ChunkGrant names its chunk by ``first`` / ``count`` *and*
the SHA-256 of the canonical JSON array of the chunk's items
(:func:`chunk_items_digest`); the lane cuts its own slice from the item list it
fetched and verified, and refuses a grant whose slice does not hash to it - the
two sides can never run a chunk over different items.

**The item list blob** (format v1 of :mod:`~meshforge.common.split_items`) is
parsed by :func:`parse_item_list`: bounded, every field type-checked, the
``files`` / ``records`` arrays parallel to ``items`` - it is read by an agent
from the network (verified by digest first) and must never crash or mislead.

**Parts index** (:func:`parts_index_blob`): every chunk's committed result
digest in chunk order, canonical JSON - the split job's result until V0.10 G5's
combine, and the combine's ordered input after it. No job id inside: the same
items, chunking and deterministic chunk outputs give the same index digest on
any node set (criterion 1).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.manifest import MAX_SPLIT_CHUNKS, MAX_SPLIT_ITEMS

__all__ = [
    "MAX_ITEM_LIST_BYTES",
    "PARTS_INDEX_VERSION",
    "ChunkSpan",
    "ItemList",
    "ItemListError",
    "chunk_items_digest",
    "chunk_payloads",
    "chunk_span",
    "parse_item_list",
    "parts_index_blob",
]

PARTS_INDEX_VERSION: Final = 1
MAX_ITEM_LIST_BYTES: Final = 512 * 1024 * 1024
"""Largest item-list blob a lane reads (100 000 items x 1 KiB + JSON)."""
_DIGEST_HEX: Final = frozenset("0123456789abcdef")
_KINDS: Final = frozenset({"glob", "list", "list_file", "range", "lines"})


class ItemListError(ValueError):
    """The item list blob is malformed (never trusted partially)."""


@dataclass(frozen=True, slots=True)
class ChunkSpan:
    first: int
    count: int

    @property
    def stop(self) -> int:
        return self.first + self.count

    def payload(self) -> dict[str, int]:
        return {"first": self.first, "count": self.count}


def chunk_payloads(item_count: int, chunk_items: int) -> list[dict[str, int]]:
    """One work-queue payload per chunk, in chunk order."""
    if not 1 <= item_count <= MAX_SPLIT_ITEMS:
        raise ValueError(f"item_count must be 1..{MAX_SPLIT_ITEMS}")
    if chunk_items < 1:
        raise ValueError("chunk_items must be >= 1")
    chunks = math.ceil(item_count / chunk_items)
    if chunks > MAX_SPLIT_CHUNKS:
        raise ValueError(f"{chunks} chunks; at most {MAX_SPLIT_CHUNKS}")
    return [
        ChunkSpan(first, min(chunk_items, item_count - first)).payload()
        for first in range(0, item_count, chunk_items)
    ]


def chunk_span(payload: Any, item_count: int) -> ChunkSpan:
    """A work-queue payload back to its span (validated against the list)."""
    if not isinstance(payload, Mapping):
        raise TypeError("a chunk payload is an object")
    first, count = payload.get("first"), payload.get("count")
    if not (
        isinstance(first, int)
        and isinstance(count, int)
        and not isinstance(first, bool)
        and not isinstance(count, bool)
        and first >= 0
        and count >= 1
        and first + count <= item_count
    ):
        raise ValueError("a chunk payload is out of the item list")
    return ChunkSpan(first, count)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def chunk_items_digest(items: Sequence[str | int]) -> Digest:
    """SHA-256 of the canonical JSON array of one chunk's items."""
    return sha256_bytes(_canonical(list(items)))


@dataclass(frozen=True, slots=True)
class ItemList:
    """A parsed item list blob (format v1)."""

    over: str
    items: tuple[str | int, ...]
    files: tuple[tuple[str, int], ...] = ()
    """``glob``: ``(sha256 hex, size)`` per item."""
    source: tuple[str, str, int] | None = None
    """``list_file`` / ``lines``: ``(path, sha256 hex, size)`` of the file."""
    records: tuple[tuple[int, int], ...] = ()
    """``lines``: ``(offset, length)`` per item."""
    header: tuple[int, int] | None = None

    @property
    def count(self) -> int:
        return len(self.items)

    def chunk(self, span: ChunkSpan) -> tuple[str | int, ...]:
        if span.stop > self.count:
            raise ValueError("the chunk is out of the item list")
        return self.items[span.first : span.stop]


def _hex(value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _DIGEST_HEX:
        raise ItemListError(f"{what} is not a sha256 hex")
    return value


def _uint(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ItemListError(f"{what} is not a non-negative integer")
    return value


def _pairs(value: Any, n: int, what: str) -> list[Any]:
    if not isinstance(value, list) or len(value) != n:
        raise ItemListError(f"{what} must have one entry per item")
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ItemListError(f"{what} entries are pairs")
    return value


def parse_item_list(data: bytes) -> ItemList:
    """Parse and check an item list blob (module docstring)."""
    if len(data) > MAX_ITEM_LIST_BYTES:
        raise ItemListError("the item list is too large")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ItemListError("the item list is not UTF-8 JSON") from None
    if not isinstance(doc, dict) or doc.get("v") != 1:
        raise ItemListError("not an item list of version 1")
    over, raw = doc.get("over"), doc.get("items")
    if over not in _KINDS or not isinstance(raw, list):
        raise ItemListError("the item list has no kind or no items")
    if not 1 <= len(raw) <= MAX_SPLIT_ITEMS or doc.get("count") != len(raw):
        raise ItemListError("the item count is wrong")
    items: list[str | int] = []
    for value in raw:
        if over in ("range", "lines"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ItemListError("range / lines items are integers")
        elif not isinstance(value, str) or not value or "\x00" in value:
            raise ItemListError("items are non-empty strings")
        items.append(value)
    files: tuple[tuple[str, int], ...] = ()
    records: tuple[tuple[int, int], ...] = ()
    header: tuple[int, int] | None = None
    source: tuple[str, str, int] | None = None
    if over == "glob":
        files = tuple(
            (_hex(h, "files sha256"), _uint(n, "files size"))
            for h, n in _pairs(doc.get("files"), len(items), "files")
        )
    if over in ("list_file", "lines"):
        src = doc.get("source")
        if not isinstance(src, dict) or not isinstance(src.get("path"), str):
            raise ItemListError("the source file is missing")
        source = (
            src["path"],
            _hex(src.get("sha256"), "source sha256"),
            _uint(src.get("size"), "source size"),
        )
    if over == "lines":
        assert source is not None  # noqa: S101 - set just above
        records = tuple(
            (_uint(o, "record offset"), _uint(n, "record length"))
            for o, n in _pairs(doc.get("records"), len(items), "records")
        )
        head = doc.get("header")
        if head is not None:
            pair = _pairs([head], 1, "header")[0]
            header = (_uint(pair[0], "header offset"), _uint(pair[1], "header size"))
        for offset, length in (*records, *([header] if header else [])):
            if length < 1 or offset + length > source[2]:
                raise ItemListError("a record is outside its file")
    return ItemList(
        over=str(over),
        items=tuple(items),
        files=files,
        source=source,
        records=records,
        header=header,
    )


def parts_index_blob(
    *, items_digest: str, chunk_items: int, chunks: Sequence[tuple[int, int, str]]
) -> bytes:
    """The parts index (module docstring): ``chunks`` = ``(first, count,
    result digest)`` in chunk order."""
    return _canonical(
        {
            "v": PARTS_INDEX_VERSION,
            "items_digest": items_digest,
            "chunk_items": chunk_items,
            "chunks": [[first, count, digest] for first, count, digest in chunks],
        }
    )
