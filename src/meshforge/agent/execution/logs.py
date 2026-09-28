"""An attempt's output: local log files plus live ``LogChunk`` batches (V0.4 D71).

Every byte the workload writes goes to ``<workdir>/.meshforge/{stdout,stderr}.log``
(the authoritative copy, uploaded as the final log) **and** into a per-stream
batch that becomes a live ``LogChunk``:

* a batch is flushed when it reaches ``flush_bytes`` (64 KiB) or when its oldest
  byte has waited ``flush_s`` (100 ms) - :class:`LogBatcher`, clock injected;
* every chunk carries the byte ``offset`` of its data in the stream, so the
  controller can skip a duplicate and record (never fill) a gap;
* **cap.** Past ``max_bytes`` per stream the file stops growing, the stream is
  marked ``truncated`` and one marker line is appended - to the file and to the
  live stream - so the truncation is visible in the log itself, never silent.

Nothing here touches the network: the executor hands the chunks to the session
client's bounded log queue (``AgentSessionClient.send_log``), which drops them
when full or offline - the final upload is what makes the log complete.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

from meshforge.agent.execution.runtime import Stream

__all__ = [
    "FLUSH_BYTES",
    "FLUSH_S",
    "AttemptLogs",
    "Chunk",
    "LogBatcher",
    "file_sha256",
    "truncation_marker",
]

FLUSH_BYTES: Final = 64 * 1024
FLUSH_S: Final = 0.1
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)


def truncation_marker(max_bytes: int) -> bytes:
    return (
        f"\n[meshforge: output truncated - this stream exceeded {max_bytes} bytes; "
        "the rest was not kept]\n"
    ).encode()


@dataclass(frozen=True, slots=True)
class Chunk:
    stream: Stream
    offset: int
    data: bytes


@dataclass(slots=True)
class _Batch:
    offset: int = 0
    """Stream offset of ``buf[0]``."""
    buf: bytearray = field(default_factory=bytearray)
    since: float | None = None


class LogBatcher:
    """Per-stream batching with byte offsets (see module docstring)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        flush_bytes: int = FLUSH_BYTES,
        flush_s: float = FLUSH_S,
    ) -> None:
        self._clock = clock
        self._flush_bytes = flush_bytes
        self._flush_s = flush_s
        self._batches = {s: _Batch() for s in Stream}

    def add(self, stream: Stream, data: bytes) -> list[Chunk]:
        """Buffer ``data``; return the full chunks that are ready now."""
        if not data:
            return []
        batch = self._batches[stream]
        if batch.since is None:
            batch.since = self._clock()
        batch.buf += data
        out: list[Chunk] = []
        while len(batch.buf) >= self._flush_bytes:
            piece = bytes(batch.buf[: self._flush_bytes])
            del batch.buf[: self._flush_bytes]
            out.append(Chunk(stream, batch.offset, piece))
            batch.offset += len(piece)
        if not batch.buf:
            batch.since = None
        return out

    def due(self) -> list[Chunk]:
        """Batches whose oldest byte waited ``flush_s`` or more."""
        now = self._clock()
        return [
            chunk
            for stream, batch in self._batches.items()
            if batch.since is not None and now - batch.since >= self._flush_s
            for chunk in self._take(stream)
        ]

    def flush(self) -> list[Chunk]:
        """Everything buffered, now (the run ended)."""
        return [chunk for stream in Stream for chunk in self._take(stream)]

    def next_deadline(self) -> float | None:
        pending = [b.since for b in self._batches.values() if b.since is not None]
        return min(pending) + self._flush_s if pending else None

    def _take(self, stream: Stream) -> list[Chunk]:
        batch = self._batches[stream]
        if not batch.buf:
            return []
        chunk = Chunk(stream, batch.offset, bytes(batch.buf))
        batch.offset += len(chunk.data)
        batch.buf.clear()
        batch.since = None
        return [chunk]


class AttemptLogs:
    """The two local log files of one attempt and their live batches."""

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes: int,
        clock: Callable[[], float],
    ) -> None:
        directory.mkdir(exist_ok=True)
        self.directory = directory
        self.max_bytes = max_bytes
        self.batcher = LogBatcher(clock=clock)
        self._files: dict[Stream, IO[bytes]] = {}
        self.kept = dict.fromkeys(Stream, 0)
        """Workload bytes kept per stream (the marker line not included)."""
        self.truncated = dict.fromkeys(Stream, False)
        for stream in Stream:
            fd = os.open(
                self.path(stream),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW,
                0o600,
            )
            self._files[stream] = os.fdopen(fd, "ab", buffering=0)

    def path(self, stream: Stream) -> Path:
        return self.directory / f"{stream.value}.log"

    def write(self, stream: Stream, data: bytes) -> list[Chunk]:
        """Keep ``data`` (up to the cap); return the live chunks ready now."""
        if self.truncated[stream] or not data:
            return []
        room = self.max_bytes - self.kept[stream]
        kept = data[:room]
        if len(data) > room:
            kept += truncation_marker(self.max_bytes)
            self.truncated[stream] = True
        self._files[stream].write(kept)
        self.kept[stream] += min(len(data), room)
        return self.batcher.add(stream, kept)

    def close(self) -> None:
        for fh in self._files.values():
            with contextlib.suppress(OSError):
                fh.close()


def file_sha256(path: Path) -> tuple[str, int]:
    """``("sha256:<hex>", size)`` of a file (blocking)."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while block := fh.read(1024 * 1024):
            hasher.update(block)
            size += len(block)
    return f"sha256:{hasher.hexdigest()}", size
