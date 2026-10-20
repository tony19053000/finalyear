"""Job warnings that do not fail a job (V0.10 G5, D269; ``job.warnings``).

A warning is stored once per **code** on the job row, as a small JSON object:

    {"code": "nondeterministic_chunk", "count": 3, "chunks": [4, 17, 90],
     "detail": "..."}

``count`` is every occurrence; ``chunks`` the first :data:`MAX_CHUNKS_LISTED`
chunk indices (sorted, unique) - so a job whose 10 000 chunks all disagree
still has a row of bounded size. Pure functions: the caller holds the job row
locked and writes the returned list in its own transaction.

**nondeterministic_chunk** (D269): ``split.deterministic`` is on (the default)
and a second result arrived for a chunk that is already committed, with a
different digest (a speculative copy or a late retry). The committed result
(the winner) is kept; the job still succeeds - but the user is told that the
same chunk gave two different outputs, so "identical output" cannot be
promised for this workload. G6 produces such duplicates (speculation); G5 adds
the storage, the hook and the surface (API, CLI, dashboard).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "MAX_CHUNKS_LISTED",
    "NONDETERMINISTIC_CHUNK",
    "WARNING_CODES",
    "add_chunk_warning",
    "parse_warnings",
]

NONDETERMINISTIC_CHUNK: Final = "nondeterministic_chunk"
WARNING_CODES: Final = frozenset({NONDETERMINISTIC_CHUNK})
MAX_CHUNKS_LISTED: Final = 64
_DETAILS: Final = {
    NONDETERMINISTIC_CHUNK: (
        "a chunk gave two different results (split.deterministic is on); "
        "the first committed result was kept"
    ),
}


def parse_warnings(raw: Any) -> list[dict[str, Any]]:
    """The stored list, every entry checked (a malformed one is dropped:
    warnings are advisory, a bad row must never break a job read)."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw[: len(WARNING_CODES)]:
        if not isinstance(entry, Mapping):
            continue
        kind, count, chunks = entry.get("code"), entry.get("count"), entry.get("chunks")
        if kind not in WARNING_CODES:
            continue
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            continue
        listed = sorted(
            {
                c
                for c in (chunks if isinstance(chunks, list) else [])
                if isinstance(c, int) and not isinstance(c, bool) and c >= 0
            }
        )[:MAX_CHUNKS_LISTED]
        out.append(
            {
                "code": str(kind),
                "count": count,
                "chunks": listed,
                "detail": _DETAILS[str(kind)],
            }
        )
    return out


def add_chunk_warning(
    raw: Any, kind: str, chunk: int
) -> tuple[list[dict[str, Any]], bool]:
    """``raw`` with one more occurrence of warning ``kind`` for ``chunk``; the
    bool says whether this chunk is new to the warning (first time listed or
    counted past the list)."""
    if kind not in WARNING_CODES:
        raise ValueError(f"unknown warning code {kind!r}")
    current = parse_warnings(raw)
    for entry in current:
        if entry.get("code") == kind:
            chunks: Sequence[int] = entry["chunks"]
            fresh = chunk not in chunks
            entry["count"] = int(entry["count"]) + 1
            if fresh and len(chunks) < MAX_CHUNKS_LISTED:
                entry["chunks"] = sorted({*chunks, chunk})
            return current, fresh
    current.append(
        {"code": kind, "count": 1, "chunks": [chunk], "detail": _DETAILS[kind]}
    )
    return current, True
