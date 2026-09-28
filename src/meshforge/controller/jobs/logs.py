"""Attempt logs on the controller: live append-by-offset, fan-out, final copy (D71/D72).

**Live path.** ``LogChunk`` messages reach :meth:`LogHub.on_chunk` on the session
reader task. They are checked (bounds, a known *open* attempt, its node, its
exact fencing token - anything else is dropped and counted) and queued for the
hub's **one writer task** (:meth:`LogHub.run`); a full queue drops the chunk
(the offset jump then shows as a gap) so a log flood can never slow a reader.
The writer merges each chunk into ``<logs dir>/<attempt_id>.<stream>`` by offset
(:class:`StreamMerge`): a duplicate or already-covered range is skipped, an
overlap keeps only its new tail, a jump is **recorded** as a gap ``(from, to)`` -
never filled with invented bytes (the live file is the bytes received, in
order). Every appended piece is fanned out to the attempt's subscribers. A
failed write (disk full, permissions, a symlink planted at the path - opened
``O_NOFOLLOW``) is **never raised**: it is logged (``controller.logs.write_failed``,
once per failing run), counted, the stream's handle is closed and the unwritten
range is recorded as a gap; the next chunk re-opens the file. One bad file
cannot stop the writer task - whose failure would stop the controller.

**Final path.** When the attempt ends, :meth:`LogHub.finalize` (queued behind
the live chunks) replaces each live file whose final log the agent uploaded
under the attempt's grant with the verified blob bytes (the complete log), and
tells the subscribers the attempt is over (the WebSocket's end frame). An attempt
that ended without final logs (lost, cancelled) keeps its live files and their
gaps.

**Subscribers** get a bounded queue each; one that falls behind is marked
``overflowed`` and dropped - the WebSocket closes it with a typed reason; the
writer never waits for a client. At most ``max_subscribers`` per job.

All state is owned by the event loop (single-threaded); the writer's file
writes are small appends to the page cache, the final copy runs in a thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import secrets
import shutil
import stat
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.blobs import BlobStore
from meshforge.controller.session.live import LiveSession

__all__ = [
    "MAX_CHUNK_BYTES",
    "MAX_PENDING_FINALS",
    "STREAMS",
    "AttemptLogState",
    "LogEvent",
    "LogHub",
    "LogHubStats",
    "StreamMerge",
    "Subscription",
]

log = get_logger(__name__)

STREAMS: Final = ("stdout", "stderr")
MAX_CHUNK_BYTES: Final = 64 * 1024
MAX_GAPS_KEPT: Final = 1024
MAX_PENDING_FINALS: Final = 4096
"""Attempt ends queued for the writer at once. One is queued per ended attempt,
so this is only reached if the writer is stalled (e.g. a hung final copy) while
thousands of attempts end; past it an end is applied at once without the final
copy (the live file and its gaps stay; the followers still get their end frame),
so the queue - ``queue_chunks + queue_finals`` items - is bounded."""
_ENDED_KEPT: Final = 4096
_PROTO_STREAM: Final = {
    agent_pb2.LogChunk.STREAM_STDOUT: "stdout",
    agent_pb2.LogChunk.STREAM_STDERR: "stderr",
}


# ------------------------------------------------------------------ merge


@dataclass(slots=True)
class StreamMerge:
    """Offset bookkeeping of one live stream (pure; see module docstring)."""

    received_to: int = 0
    """Stream offset just past the last byte appended."""
    file_len: int = 0
    """Bytes in the live file (= received bytes; gaps are not in it)."""
    gaps: list[tuple[int, int]] = field(default_factory=list)
    gaps_total: int = 0
    write_failing: bool = False
    """The last live-file write failed (logged once per failing run)."""

    def accept(self, offset: int, data: bytes) -> bytes:
        """The part of ``data`` to append (``b""`` = nothing new)."""
        end = offset + len(data)
        if end <= self.received_to:
            return b""  # duplicate / replayed range
        if offset > self.received_to:
            self.gaps_total += 1
            if len(self.gaps) < MAX_GAPS_KEPT:
                self.gaps.append((self.received_to, offset))
            piece = data
        else:
            piece = data[self.received_to - offset :]  # overlap: new tail only
        self.received_to = end
        self.file_len += len(piece)
        return piece


# -------------------------------------------------------------- the hub


@dataclass(frozen=True, slots=True)
class LogEvent:
    kind: str
    """``data`` or ``end``."""
    stream: str = ""
    file_offset: int = 0
    data: bytes = b""


class Subscription:
    """One reader of an attempt's logs (a WebSocket)."""

    def __init__(self, hub: LogHub, attempt_id: str, job_id: str, size: int) -> None:
        self.hub = hub
        self.attempt_id = attempt_id
        self.job_id = job_id
        self.queue: asyncio.Queue[LogEvent] = asyncio.Queue(size)
        self.overflowed = False
        self.wake = asyncio.Event()

    def push(self, event: LogEvent) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True
            self.hub.stats.slow_subscribers += 1
        self.wake.set()

    def close(self) -> None:
        self.hub.unsubscribe(self)


@dataclass(slots=True)
class AttemptLogState:
    job_id: str
    node_id: str
    fencing_token: int
    streams: dict[str, StreamMerge] = field(
        default_factory=lambda: {s: StreamMerge() for s in STREAMS}
    )
    files: dict[str, IO[bytes]] = field(default_factory=dict)
    subscribers: set[Subscription] = field(default_factory=set)
    ended: bool = False
    final: dict[str, bool] = field(default_factory=dict)
    """stream -> the live file was replaced by the verified final log."""

    def gaps(self) -> dict[str, list[list[int]]]:
        return {s: [list(g) for g in m.gaps] for s, m in self.streams.items() if m.gaps}


@dataclass(slots=True)
class LogHubStats:
    chunks_appended: int = 0
    chunks_duplicate: int = 0
    chunks_fenced: int = 0
    chunks_rejected: int = 0
    chunks_dropped: int = 0
    gaps: int = 0
    finals_copied: int = 0
    slow_subscribers: int = 0
    write_failures: int = 0
    finals_overflowed: int = 0


@dataclass(frozen=True, slots=True)
class _Chunk:
    attempt_id: str
    stream: str
    offset: int
    data: bytes


@dataclass(frozen=True, slots=True)
class _Final:
    attempt_id: str
    logs: Mapping[str, Mapping[str, Any]]


class LogHub:
    """See module docstring. ``run()`` is one task owned by ``ControllerRuntime``."""

    def __init__(
        self,
        root: Path,
        *,
        blobs: BlobStore | None,
        max_bytes: int,
        queue_chunks: int = 4096,
        queue_finals: int = MAX_PENDING_FINALS,
        subscriber_queue: int = 1024,
        max_subscribers: int = 8,
    ) -> None:
        self.root = root
        self._blobs = blobs
        self._max_bytes = max_bytes
        self._queue_chunks = queue_chunks
        self._subscriber_queue = subscriber_queue
        self._max_subscribers = max_subscribers
        self._queue_finals = queue_finals
        # Bounded by construction: on_chunk and finalize admit at most
        # queue_chunks / queue_finals items, so put_nowait never raises.
        self._queue: asyncio.Queue[_Chunk | _Final] = asyncio.Queue(
            queue_chunks + queue_finals
        )
        self._pending_chunks = 0
        self._pending_finals = 0
        self._attempts: dict[str, AttemptLogState] = {}
        self._ended: OrderedDict[str, AttemptLogState] = OrderedDict()
        self._by_job: dict[str, set[Subscription]] = {}
        self.stats = LogHubStats()

    def prepare(self) -> None:
        """Create the logs folder (blocking; once at start)."""
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self.root.chmod(0o700)

    def path(self, attempt_id: str, stream: str) -> Path:
        return self.root / f"{attempt_id}.{stream}"

    # ----------------------------------------------------------- lifecycle

    def open(self, attempt_id: str, *, job_id: str, node_id: str, token: int) -> None:
        """Accept live chunks for this attempt (dispatch, re-send, Reconcile)."""
        state = self._attempts.get(attempt_id)
        if state is not None:
            state.fencing_token, state.node_id = token, node_id
            return
        state = AttemptLogState(job_id=job_id, node_id=node_id, fencing_token=token)
        for stream in STREAMS:
            # After a controller restart the file already holds what arrived
            # before; continue after it (earlier gaps are not known any more).
            with contextlib.suppress(OSError):
                info = self.path(attempt_id, stream).lstat()
                size = info.st_size if stat.S_ISREG(info.st_mode) else 0
                state.streams[stream].received_to = size
                state.streams[stream].file_len = size
        self._attempts[attempt_id] = state
        for sub in self._by_job.get(job_id, ()):
            if sub.attempt_id == attempt_id:
                state.subscribers.add(sub)
                sub.wake.set()

    def finalize(
        self, attempt_id: str, logs: Mapping[str, Mapping[str, Any]] | None
    ) -> None:
        """The attempt ended; ``logs`` = verified final logs per stream (or none).
        Queued behind the live chunks already received; past
        :data:`MAX_PENDING_FINALS` queued ends it is applied at once, without
        the final copy (see there)."""
        if self._pending_finals >= self._queue_finals:
            self.stats.finals_overflowed += 1
            state = self._attempts.pop(attempt_id, None)
            log.warning(
                "controller.logs.final_overflow",
                attempt_id=attempt_id,
                job_id=None if state is None else state.job_id,
                pending=self._pending_finals,
            )
            if state is not None:
                _close_files(state)
                self._mark_ended(attempt_id, state)
            return
        self._pending_finals += 1
        self._queue.put_nowait(_Final(attempt_id, dict(logs or {})))

    def ended_state(self, attempt_id: str) -> AttemptLogState | None:
        return self._ended.get(attempt_id)

    def live_state(self, attempt_id: str) -> AttemptLogState | None:
        return self._attempts.get(attempt_id)

    # ------------------------------------------------------------- input

    async def on_chunk(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        """Session dispatch handler for ``log_chunk`` (never acked, D71)."""
        chunk = msg.log_chunk
        stream = _PROTO_STREAM.get(chunk.stream)
        if (
            stream is None
            or len(chunk.data) > MAX_CHUNK_BYTES
            or chunk.offset + len(chunk.data) > self._max_bytes
        ):
            self.stats.chunks_rejected += 1
            return
        state = self._attempts.get(chunk.attempt_id)
        if (
            state is None
            or state.ended
            or state.node_id != live.node_id
            or state.fencing_token != chunk.fencing_token
        ):
            self.stats.chunks_fenced += 1
            return
        if self._pending_chunks >= self._queue_chunks:
            self.stats.chunks_dropped += 1
            return
        self._pending_chunks += 1
        self._queue.put_nowait(
            _Chunk(chunk.attempt_id, stream, int(chunk.offset), bytes(chunk.data))
        )

    # ------------------------------------------------------------ writer

    async def run(self) -> None:
        """The one writer: append live chunks, apply finals, fan out."""
        try:
            while True:
                item = await self._queue.get()
                if isinstance(item, _Chunk):
                    self._pending_chunks -= 1
                    self._append(item)
                else:
                    self._pending_finals -= 1
                    await self._apply_final(item)
        finally:
            for state in self._attempts.values():
                _close_files(state)

    def _append(self, item: _Chunk) -> None:
        state = self._attempts.get(item.attempt_id)
        if state is None or state.ended:
            self.stats.chunks_fenced += 1
            return
        merge = state.streams[item.stream]
        gaps_before = merge.gaps_total
        start = merge.file_len
        piece = merge.accept(item.offset, item.data)
        if merge.gaps_total != gaps_before:
            self.stats.gaps += 1
            log.info(
                "controller.logs.gap",
                attempt_id=item.attempt_id,
                job_id=state.job_id,
                stream=item.stream,
                gap_from=merge.gaps[-1][0] if merge.gaps else None,
                gap_to=item.offset,
            )
        if not piece:
            self.stats.chunks_duplicate += 1
            return
        written = 0
        try:
            fh = state.files.get(item.stream)
            if fh is None:
                fh = self._open_live(item.attempt_id, item.stream)
                state.files[item.stream] = fh
            while written < len(piece):
                written += _write_some(fh, piece[written:])
        except OSError as exc:
            self._write_failed(item, state, merge, piece, written, exc)
            if not written:
                return
            piece = piece[:written]
        else:
            merge.write_failing = False
        self.stats.chunks_appended += 1
        event = LogEvent("data", item.stream, start, piece)
        for sub in list(state.subscribers):
            sub.push(event)

    def _open_live(self, attempt_id: str, stream: str) -> IO[bytes]:
        fd = os.open(
            self.path(attempt_id, stream),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _require_regular(fd)
            return os.fdopen(fd, "ab", buffering=0)
        except BaseException:
            os.close(fd)
            raise

    def _write_failed(
        self,
        item: _Chunk,
        state: AttemptLogState,
        merge: StreamMerge,
        piece: bytes,
        written: int,
        exc: OSError,
    ) -> None:
        """A live-file write failed (disk full, permissions, a planted symlink):
        the unwritten bytes become a recorded gap, the handle is closed (the
        next chunk re-opens - a full disk may recover) and the hub carries on.
        Never raised: one bad file must not stop the writer (and the controller).
        """
        self.stats.write_failures += 1
        fh = state.files.pop(item.stream, None)
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()
        lost = len(piece) - written
        merge.file_len -= lost
        lost_from = merge.received_to - lost
        if merge.gaps and merge.gaps[-1][1] == lost_from:
            merge.gaps[-1] = (merge.gaps[-1][0], merge.received_to)
        else:
            merge.gaps_total += 1
            self.stats.gaps += 1
            if len(merge.gaps) < MAX_GAPS_KEPT:
                merge.gaps.append((lost_from, merge.received_to))
        if not merge.write_failing:
            merge.write_failing = True
            log.warning(
                "controller.logs.write_failed",
                attempt_id=item.attempt_id,
                job_id=state.job_id,
                stream=item.stream,
                cause=type(exc).__name__,
                errno=exc.errno,
            )

    async def _apply_final(self, item: _Final) -> None:
        state = self._attempts.pop(item.attempt_id, None)
        if state is None:
            state = self._ended.get(item.attempt_id)
            if state is None:
                return
        _close_files(state)
        for stream, info in item.logs.items():
            digest = info.get("digest")
            if stream not in STREAMS or not digest or self._blobs is None:
                continue
            try:
                source = self._blobs.path_for(Digest.parse(str(digest)))
                await asyncio.to_thread(
                    _replace_with, source, self.path(item.attempt_id, stream)
                )
            except (OSError, ValueError) as exc:
                log.warning(
                    "controller.logs.final_copy_failed",
                    attempt_id=item.attempt_id,
                    job_id=state.job_id,
                    stream=stream,
                    cause=type(exc).__name__,
                )
                continue
            state.final[stream] = True
            self.stats.finals_copied += 1
        self._mark_ended(item.attempt_id, state)

    def _mark_ended(self, attempt_id: str, state: AttemptLogState) -> None:
        state.ended = True
        self._ended[attempt_id] = state
        while len(self._ended) > _ENDED_KEPT:
            self._ended.popitem(last=False)
        for sub in list(state.subscribers):
            sub.push(LogEvent("end"))

    # -------------------------------------------------------- subscribers

    def subscribe(self, attempt_id: str, job_id: str) -> Subscription | None:
        """A new subscriber, or ``None`` when the job has ``max_subscribers``."""
        subs = self._by_job.setdefault(job_id, set())
        if len(subs) >= self._max_subscribers:
            return None
        sub = Subscription(self, attempt_id, job_id, self._subscriber_queue)
        subs.add(sub)
        state = self._attempts.get(attempt_id) or self._ended.get(attempt_id)
        if state is not None:
            state.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._by_job.get(sub.job_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._by_job[sub.job_id]
        for table in (self._attempts, self._ended):
            state = table.get(sub.attempt_id)
            if state is not None:
                state.subscribers.discard(sub)

    def file_len(self, attempt_id: str, stream: str) -> int:
        """Bytes persisted now (what a new subscriber reads before live events)."""
        state = self._attempts.get(attempt_id)
        if state is not None and not state.ended:
            return state.streams[stream].file_len
        try:
            return self.path(attempt_id, stream).stat().st_size
        except OSError:
            return 0


def _close_files(state: AttemptLogState) -> None:
    for fh in state.files.values():
        with contextlib.suppress(OSError):
            fh.close()
    state.files.clear()


def _write_some(fh: IO[bytes], data: bytes) -> int:
    """One unbuffered write; a write that stores nothing is an error (``EIO``)."""
    n = fh.write(data)
    if not n:
        raise OSError(errno.EIO, "short write")
    return n


def _require_regular(fd: int) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise OSError(errno.EINVAL, "not a regular file")


def _replace_with(source: Path, dest: Path) -> None:
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
