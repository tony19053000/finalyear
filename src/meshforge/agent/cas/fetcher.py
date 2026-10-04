"""Fetch blobs into the agent's CAS from several sources at once (V0.5 G1 D80/D84,
G4 D89).

**Units of work.** Every blob a tree names that the cache does not hold becomes
work units: a **one-chunk** file (``size <= chunk_size``) is one unit - a ``GET``
streamed into a :class:`~meshforge.common.cas.BlobWriter` that hashes as it
writes and installs the blob only if the digest matches; a **chunked** file is one
unit per *missing* chunk of its :class:`~meshforge.common.cas.ResumablePart`
(``Range: bytes=a-b``). Every chunk is verified against the manifest's chunk
digest before it is written, the bitmap sidecar records it, and the assembled
file is hashed whole before it becomes a blob (checklist 15). A crash or a
cancellation keeps the verified chunks: the next fetch asks only for the
missing ones (criterion 3).

**Work stealing across sources (D89).** The sources are the controller and the
peers that hold the tree (``ExecutionSpec.sources``). Each source runs
``per_source`` workers that take the next unit from one shared queue, so a faster
source simply takes more units - no ranking is guessed. When the queue is empty
an idle worker *duplicates* a unit that has been in flight on another source for
longer than ``endgame_after_s`` (end-game mode); the first verified answer wins
and the other request is cancelled, so one slow source cannot hold the last
chunks hostage.

**Failures** (a source is *dropped* for this transfer, its in-flight units go
back to the queue):

* a peer that answers 401/403, fails, times out, sends a corrupt chunk or blob,
  or is slower than ``slow_source_factor`` x the median rate of the sources
  (after ``slow_min_units`` units each) is dropped - **at once, no waiting for a
  controller refresh**;
* a 404 drops the source for *that blob* only (a holder that lost a blob);
* the controller is never dropped for slowness - it is the last resort. A corrupt
  chunk from it is asked again up to ``chunk_retries`` times, a corrupt one-chunk
  blob once per blob within the staging's ``max_refetches`` budget; after that
  (and on its 401/403, 404, timeout, transport error) it is excluded too.

A unit no remaining source can serve fails the fetch with the controller's typed
error when the controller was involved (``unauthorized`` 401/403 - the caller
then waits for ``RefreshFetchToken``, D95 - ``not_found``, ``integrity_error``,
``timeout``, ``transfer_error``), else ``transfer_error``. Nothing of a failed
blob is ever installed. Tokens travel only in the ``Authorization`` header and
are never logged.

``FetchStats.sources`` has the bytes, request time and units per source (G5
ships them as transfer telemetry, D93).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import statistics
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

import httpx

from meshforge.agent.cas.store import AgentCas
from meshforge.common.cas import (
    CasError,
    CasIntegrityError,
    ResumablePart,
    chunk_span,
)
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.tree import Tree, TreeEntry, TreeError, TreeLimits, parse_tree

__all__ = [
    "BlobFetcher",
    "BlobSource",
    "FetchError",
    "FetchStats",
    "FetchTuning",
    "SourceStats",
]

log = get_logger(__name__)

_STREAM: Final = 1024 * 1024
DEFAULT_PARALLEL: Final = 4
DEFAULT_CHUNK_RETRIES: Final = 2
DEFAULT_MAX_REFETCHES: Final = 3
"""Whole blobs one staging may fetch again after a mismatch (each blob once)."""
_CONTROLLER: Final = "controller"


class FetchError(MeshForgeError):
    default_failure_type = FailureType.TRANSFER_ERROR

    @property
    def source(self) -> str:
        """``controller`` or a peer's node id ("" when not source-specific)."""
        return str(self.details.get("source", ""))


@dataclass(frozen=True, slots=True)
class BlobSource:
    base_url: str
    """``http(s)://host:port`` (no trailing slash)."""
    token: str
    node_id: str = ""
    """Empty = the controller."""

    def __repr__(self) -> str:  # never show the token
        return f"BlobSource(base_url={self.base_url!r}, node_id={self.node_id!r})"

    @property
    def name(self) -> str:
        return self.node_id or _CONTROLLER


@dataclass(frozen=True, slots=True)
class FetchTuning:
    """``[agent.fetch]`` knobs (see :class:`~meshforge.agent.settings.FetchSettings`)."""

    per_source: int = 2
    slow_source_factor: float = 4.0
    slow_min_units: int = 2
    endgame_after_s: float = 0.5


@dataclass(slots=True)
class SourceStats:
    bytes: int = 0
    """Payload bytes received from the source (wasted duplicates included)."""
    duration_s: float = 0.0
    """Time spent in requests to it (monotonic; overlapping requests add up)."""
    units: int = 0
    """Units it won (chunks or whole blobs)."""
    failures: int = 0
    dropped: str | None = None
    """Why it was dropped for this transfer (None = it stayed)."""


@dataclass(slots=True)
class FetchStats:
    blobs_hit: int = 0
    blobs_fetched: int = 0
    bytes_fetched: int = 0
    """Payload bytes received (chunks re-requested after corruption included)."""
    chunks_fetched: int = 0
    chunks_resumed: int = 0
    """Chunks a previous, interrupted fetch had already verified and kept."""
    corrupt_chunks: int = 0
    manifest_hit: bool = False
    refetches: int = 0
    """Whole blobs fetched again after a mismatch (cached or in flight)."""
    duplicated_units: int = 0
    """End-game duplicates started (a unit asked from a second source)."""
    sources: dict[str, SourceStats] = field(default_factory=dict)
    """Per source (``controller`` or the peer's node id)."""


def _status_error(status: int, what: str, source: str) -> FetchError:
    details = {"source": source, "status": status}
    if status in (401, 403):
        return FetchError(
            f"the source refused this attempt's fetch token for {what}",
            failure_type=FailureType.UNAUTHORIZED,
            details=details,
        )
    if status == 404:
        return FetchError(
            f"the source does not have {what}",
            failure_type=FailureType.NOT_FOUND,
            details=details,
        )
    return FetchError(f"fetching {what} answered HTTP {status}", details=details)


class _Corrupt(Exception):  # noqa: N818 - internal control flow
    pass


# ================================================================ work units


@dataclass(eq=False, slots=True)
class _Source:
    src: BlobSource
    stats: SourceStats
    dropped: FetchError | None = None

    @property
    def name(self) -> str:
        return self.src.name

    @property
    def controller(self) -> bool:
        return not self.src.node_id


@dataclass(eq=False, slots=True)
class _Blob:
    entry: TreeEntry
    chunk_size: int
    part: ResumablePart | None
    remaining: int
    finished: asyncio.Future[None]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    excluded: dict[str, FetchError] = field(default_factory=dict)
    """Sources that cannot serve this blob (404, corrupt beyond retries)."""


@dataclass(eq=False, slots=True)
class _Unit:
    blob: _Blob
    index: int | None
    """Chunk index; None = the whole (one-chunk) blob."""
    done: bool = False
    attempts: dict[str, asyncio.Future[None]] = field(default_factory=dict)
    started_at: float = 0.0
    excluded: dict[str, FetchError] = field(default_factory=dict)
    corrupt: dict[str, int] = field(default_factory=dict)

    def barred(self, name: str) -> bool:
        return name in self.excluded or name in self.blob.excluded


class BlobFetcher:
    """See module docstring. One per staging; ``client`` is the caller's."""

    def __init__(
        self,
        cas: AgentCas,
        sources: Iterable[BlobSource],
        client: httpx.AsyncClient,
        *,
        parallel: int = DEFAULT_PARALLEL,
        chunk_retries: int = DEFAULT_CHUNK_RETRIES,
        tree_limits: TreeLimits | None = None,
        max_refetches: int = DEFAULT_MAX_REFETCHES,
        tuning: FetchTuning | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        listed = list(sources)
        if not listed:
            raise ValueError("at least one blob source is needed")
        if parallel < 1 or chunk_retries < 0:
            raise ValueError("parallel >= 1 and chunk_retries >= 0")
        self._cas = cas
        self._client = client
        self._parallel = parallel
        self._retries = chunk_retries
        self._limits = tree_limits or TreeLimits()
        self._tuning = tuning or FetchTuning()
        if self._tuning.per_source < 1:
            raise ValueError("per_source >= 1")
        self._clock = clock
        self.stats = FetchStats()
        self._refetch_left = max_refetches
        self._refetched: set[str] = set()
        self._sources: list[_Source] = []
        seen: set[str] = set()
        for src in listed:
            if src.name in seen:
                continue  # one entry per source (a duplicate listing adds nothing)
            seen.add(src.name)
            stats = self.stats.sources.setdefault(src.name, SourceStats())
            self._sources.append(_Source(src, stats))
        self._queue: deque[_Unit] = deque()
        self._inflight: list[_Unit] = []
        self._changed = asyncio.Condition()
        self._running = False

    # ---------------------------------------------------------- the sources

    @property
    def sources(self) -> list[BlobSource]:
        return [s.src for s in self._sources]

    def _active(self) -> list[_Source]:
        return [s for s in self._sources if s.dropped is None]

    def _drop(self, source: _Source, error: FetchError) -> None:
        if source.dropped is not None:
            return
        source.dropped = error
        source.stats.dropped = error.failure_type.value
        self._check_all()
        log.warning(
            "agent.cas.source_dropped",
            source=source.name,
            reason=error.failure_type.value,
            detail=error.message[:200],
        )

    def _count(self, source: _Source, n: int) -> None:
        self.stats.bytes_fetched += n
        source.stats.bytes += n

    @staticmethod
    def _headers(
        source: _Source, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        return {"authorization": f"Bearer {source.src.token}", **(extra or {})}

    @staticmethod
    def _url(source: _Source, digest: Digest) -> str:
        return f"{source.src.base_url}/v1/blobs/{digest.hex}"

    # ------------------------------------------------------------- manifest

    async def tree(self, tree_id: Digest) -> Tree:
        """The verified tree ``tree_id`` (from the cache, else fetched + cached).

        Asked from each source in the listed order until one answers with bytes
        that hash to ``tree_id``; a failing peer is dropped for the transfer.
        """
        store = self._cas.store
        async with self._cas.holding(tree_id):
            if store.has(tree_id):
                data = await asyncio.to_thread(store.path_for(tree_id).read_bytes)
                try:
                    tree = parse_tree(data, expected=tree_id, limits=self._limits)
                except TreeError:
                    # A cached manifest that no longer verifies: evict, refetch.
                    await asyncio.to_thread(store.evict, tree_id)
                else:
                    self.stats.manifest_hit = True
                    return tree
            errors: list[FetchError] = []
            for source in self._active():
                try:
                    data = await self._get_manifest(source, tree_id)
                except FetchError as exc:
                    errors.append(exc)
                    if not source.controller:
                        self._drop(source, exc)
                        continue
                    if exc.failure_type in (
                        FailureType.INTEGRITY_ERROR,
                        FailureType.INVALID_INPUT,
                    ):
                        raise  # the controller sent something wrong: not a retry
                    continue  # unauthorized / unreachable: a peer may still help
                tree = parse_tree(data, expected=tree_id, limits=self._limits)
                try:
                    await asyncio.to_thread(store.put_bytes, data, tree_id)
                except CasError as exc:
                    raise FetchError(
                        exc.message, failure_type=exc.failure_type
                    ) from None
                return tree
            raise self._final(errors)

    async def _get_manifest(self, source: _Source, tree_id: Digest) -> bytes:
        max_bytes = self._limits.max_manifest_bytes
        started = self._clock()
        try:
            async with self._client.stream(
                "GET", self._url(source, tree_id), headers=self._headers(source)
            ) as response:
                if response.status_code != 200:
                    raise _status_error(
                        response.status_code, "the tree manifest", source.name
                    )
                data = bytearray()
                async for piece in response.aiter_raw(_STREAM):
                    data += piece
                    self._count(source, len(piece))
                    if len(data) > max_bytes:
                        raise FetchError(
                            f"tree manifest larger than {max_bytes} bytes",
                            failure_type=FailureType.INVALID_INPUT,
                            details={"source": source.name},
                        )
        except httpx.TimeoutException:
            raise FetchError(
                "timed out fetching the tree manifest",
                failure_type=FailureType.TIMEOUT,
                details={"source": source.name},
            ) from None
        except httpx.HTTPError as exc:
            raise FetchError(
                f"manifest fetch failed: {type(exc).__name__}",
                details={"source": source.name},
            ) from None
        finally:
            source.stats.duration_s += self._clock() - started
        if hashlib.sha256(data).hexdigest() != tree_id.hex:
            raise FetchError(
                "the tree manifest does not match its id; nothing was kept",
                failure_type=FailureType.INTEGRITY_ERROR,
                details={"source": source.name},
            )
        return bytes(data)

    # ---------------------------------------------------------------- blobs

    async def ensure_all(self, tree: Tree) -> FetchStats:
        """Every file blob of ``tree`` is in the cache (fetched if missing)."""
        unique: dict[str, TreeEntry] = {}
        for entry in tree.files():
            unique.setdefault(entry.sha256, entry)
        await self._run(list(unique.values()), tree.chunk_size)
        return self.stats

    async def ensure(self, entry: TreeEntry, chunk_size: int) -> bool:
        """Fetch ``entry``'s blob unless cached; True = it was fetched now."""
        before = self.stats.blobs_fetched
        await self._run([entry], chunk_size)
        return self.stats.blobs_fetched > before

    def allow_refetch(self, digest: Digest) -> bool:
        """Spend the refetch budget: each blob at most once, at most
        ``max_refetches`` per staging (D3 of the G1 fix round)."""
        if digest.hex in self._refetched or self._refetch_left <= 0:
            return False
        self._refetched.add(digest.hex)
        self._refetch_left -= 1
        self.stats.refetches += 1
        return True

    async def _run(self, entries: list[TreeEntry], chunk_size: int) -> None:
        if not entries:
            return
        if self._running:
            raise RuntimeError("one transfer at a time per fetcher")
        self._running = True
        slots = asyncio.Semaphore(
            max(self._parallel, self._tuning.per_source * len(self._sources) * 2)
        )
        try:
            async with asyncio.TaskGroup() as group:
                blobs = [
                    group.create_task(self._blob(entry, chunk_size, slots))
                    for entry in entries
                ]
                workers = [
                    group.create_task(self._worker(source))
                    for source in self._sources
                    for _ in range(self._tuning.per_source)
                ]
                try:
                    await asyncio.gather(*blobs)
                finally:
                    for worker in workers:
                        worker.cancel()
        except BaseExceptionGroup as group_error:
            # One typed error for the attempt: the first fetch failure (the
            # TaskGroup already cancelled the other fetches).
            raise _first_leaf(group_error) from None
        finally:
            self._running = False
            self._queue.clear()
            self._inflight.clear()

    async def _blob(
        self, entry: TreeEntry, chunk_size: int, slots: asyncio.Semaphore
    ) -> None:
        """Hold the blob's lock while its units are fetched (two stagings never
        fetch one blob twice), then install it."""
        digest = entry.digest
        store = self._cas.store
        async with slots, self._cas.holding(digest):
            if store.has(digest):
                self.stats.blobs_hit += 1
                return
            loop = asyncio.get_running_loop()
            if entry.chunks:
                try:
                    part = await asyncio.to_thread(
                        store.resumable, digest, entry.size, chunk_size
                    )
                except CasError as exc:
                    raise FetchError(
                        exc.message, failure_type=exc.failure_type
                    ) from None
                self.stats.chunks_resumed += part.resumed_chunks
                missing = part.missing()
                blob = _Blob(
                    entry, chunk_size, part, len(missing), loop.create_future()
                )
                units = [_Unit(blob, index) for index in missing]
            else:
                blob = _Blob(entry, chunk_size, None, 1, loop.create_future())
                units = [_Unit(blob, None)]
            if units:
                await self._enqueue(units)
                try:
                    await blob.finished
                except FetchError:
                    # Let requests of this blob already under way land first: a
                    # chunk that arrived is kept for the resume (criterion 3).
                    await _settle(units)
                    raise
                finally:
                    self._forget(blob)
            if blob.part is not None:
                try:
                    await asyncio.to_thread(blob.part.complete)
                except CasIntegrityError:
                    raise FetchError(
                        f"blob {digest} does not match its digest after assembly; "
                        "the partial download was discarded",
                        failure_type=FailureType.INTEGRITY_ERROR,
                    ) from None
                except CasError as exc:
                    raise FetchError(
                        exc.message, failure_type=exc.failure_type
                    ) from None
            self.stats.blobs_fetched += 1

    async def _enqueue(self, units: list[_Unit]) -> None:
        async with self._changed:
            self._queue.extend(units)
            self._changed.notify_all()
        for unit in units:
            self._check_servable(unit)

    def _forget(self, blob: _Blob) -> None:
        self._queue = deque(u for u in self._queue if u.blob is not blob)
        self._inflight = [u for u in self._inflight if u.blob is not blob]

    # --------------------------------------------------------------- workers

    async def _worker(self, source: _Source) -> None:
        while source.dropped is None:
            unit = await self._take(source)
            if unit is None:
                return
            await self._attempt(source, unit)

    async def _take(self, source: _Source) -> _Unit | None:
        async with self._changed:
            while True:
                if source.dropped is not None:
                    return None
                for unit in self._queue:
                    if not unit.barred(source.name):
                        self._queue.remove(unit)
                        unit.started_at = self._clock()
                        self._inflight.append(unit)
                        return unit
                stolen = self._steal(source)
                if stolen is not None:
                    return stolen
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._changed.wait(), max(0.05, self._tuning.endgame_after_s)
                    )

    def _steal(self, source: _Source) -> _Unit | None:
        """End-game: duplicate the oldest unit in flight elsewhere."""
        now = self._clock()
        for unit in self._inflight:
            if (
                not unit.done
                and len(unit.attempts) == 1
                and source.name not in unit.attempts
                and not unit.barred(source.name)
                and now - unit.started_at >= self._tuning.endgame_after_s
            ):
                self.stats.duplicated_units += 1
                return unit
        return None

    async def _attempt(self, source: _Source, unit: _Unit) -> None:
        request = asyncio.ensure_future(self._request(source, unit))
        unit.attempts[source.name] = request
        failure: FetchError | None = None
        try:
            await request
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                request.cancel()
                raise  # the worker itself is being stopped
            # Cancelled by the winner of an end-game race: nothing to do.
        except _Corrupt:
            failure = self._corrupt(source, unit)
        except FetchError as exc:
            failure = exc
            self._failed(source, unit, exc)
        finally:
            unit.attempts.pop(source.name, None)
        if failure is not None:
            source.stats.failures += 1
            self._check_all()
        if not unit.done and not unit.attempts:
            async with self._changed:
                if unit in self._inflight:
                    self._inflight.remove(unit)
                if not unit.blob.finished.done():
                    self._queue.appendleft(unit)
                self._changed.notify_all()
            self._check_servable(unit)
        self._check_slow()

    def _failed(self, source: _Source, unit: _Unit, exc: FetchError) -> None:
        if exc.failure_type is FailureType.NOT_FOUND:
            unit.blob.excluded[source.name] = exc  # this holder lost the blob
            return
        if source.controller and exc.failure_type in (
            FailureType.INVALID_INPUT,
            FailureType.DISK_FULL,
        ):
            unit.blob.excluded[source.name] = exc
            return
        self._drop(source, exc)

    def _corrupt(self, source: _Source, unit: _Unit) -> FetchError:
        self.stats.corrupt_chunks += unit.index is not None
        digest = unit.blob.entry.digest
        what = (
            f"chunk {unit.index} of blob {digest}"
            if unit.index is not None
            else f"blob {digest}"
        )
        log.warning(
            "agent.cas.chunk_corrupt"
            if unit.index is not None
            else "agent.cas.blob_refetch",
            digest=str(digest),
            chunk=unit.index,
            source=source.name,
        )
        error = FetchError(
            f"{what} arrived corrupt; nothing of it was kept",
            failure_type=FailureType.INTEGRITY_ERROR,
            details={"source": source.name},
        )
        if not source.controller:
            self._drop(source, error)  # a peer that serves bad bytes is out
            return error
        seen = unit.corrupt[source.name] = unit.corrupt.get(source.name, 0) + 1
        if unit.index is not None:
            if seen > self._retries:
                unit.excluded[source.name] = FetchError(
                    f"chunk {unit.index} of blob {digest} arrived corrupt "
                    f"{seen} times; nothing of it was kept",
                    failure_type=FailureType.INTEGRITY_ERROR,
                    details={"source": source.name},
                )
        elif not self.allow_refetch(digest):
            unit.excluded[source.name] = error
        return error

    def _check_all(self) -> None:
        """A source went away or was barred: fail what nobody can serve now."""
        for unit in [*self._queue, *self._inflight]:
            self._check_servable(unit)

    def _check_servable(self, unit: _Unit) -> None:
        if unit.done or unit.blob.finished.done() or unit.attempts:
            return
        if any(not unit.barred(s.name) for s in self._active()):
            return
        reasons = [
            unit.excluded.get(s.name) or unit.blob.excluded.get(s.name) or s.dropped
            for s in self._sources
        ]
        unit.blob.finished.set_exception(
            self._final([r for r in reasons if r is not None])
        )

    def _final(self, errors: list[FetchError]) -> FetchError:
        """The typed error of a unit nobody can serve (module docstring)."""
        for error in errors:
            if error.source == _CONTROLLER:
                return error
        if errors:
            last = errors[-1]
            return FetchError(
                f"no blob source could serve it (last: {last.message})",
                failure_type=FailureType.TRANSFER_ERROR,
            )
        return FetchError("no blob source left")

    def _check_slow(self) -> None:
        """Drop a peer slower than ``slow_source_factor`` x the median rate."""
        rated = [
            (s, s.stats.bytes / s.stats.duration_s)
            for s in self._active()
            if s.stats.units >= self._tuning.slow_min_units and s.stats.duration_s > 0
        ]
        if len(rated) < 2:
            return
        median = statistics.median(rate for _, rate in rated)
        for source, rate in rated:
            if (
                not source.controller
                and rate * self._tuning.slow_source_factor < median
            ):
                self._drop(
                    source,
                    FetchError(
                        f"the source is too slow ({rate:.0f} B/s vs median {median:.0f})",
                        details={"source": source.name},
                    ),
                )

    # -------------------------------------------------------------- requests

    async def _request(self, source: _Source, unit: _Unit) -> None:
        started = self._clock()
        try:
            if unit.index is None:
                await self._fetch_whole(source, unit)
            else:
                await self._fetch_chunk(source, unit)
        finally:
            source.stats.duration_s += self._clock() - started

    async def _won(self, source: _Source, unit: _Unit) -> None:
        unit.done = True
        source.stats.units += 1
        for name, other in list(unit.attempts.items()):
            if name != source.name:
                other.cancel()
        async with self._changed:
            if unit in self._inflight:
                self._inflight.remove(unit)
            self._changed.notify_all()
        blob = unit.blob
        blob.remaining -= 1
        if blob.remaining <= 0 and not blob.finished.done():
            blob.finished.set_result(None)

    async def _fetch_chunk(self, source: _Source, unit: _Unit) -> None:
        blob = unit.blob
        entry = blob.entry
        if unit.index is None or blob.part is None:  # pragma: no cover - invariant
            raise RuntimeError("a chunk unit without a chunk")
        start, length = chunk_span(unit.index, entry.size, blob.chunk_size)
        data = await self._range(source, entry.digest, start, length)
        async with blob.lock:
            if unit.done:
                return  # another source won the race meanwhile
            try:
                await asyncio.to_thread(
                    blob.part.write_chunk, unit.index, data, entry.chunks[unit.index]
                )
            except CasIntegrityError:
                raise _Corrupt from None
            except CasError as exc:
                raise FetchError(
                    exc.message,
                    failure_type=exc.failure_type,
                    details={"source": source.name},
                ) from None
            self.stats.chunks_fetched += 1
            await self._won(source, unit)

    async def _fetch_whole(self, source: _Source, unit: _Unit) -> None:
        blob = unit.blob
        digest = blob.entry.digest
        store = self._cas.store
        try:
            with store.writer(digest, blob.entry.size) as writer:
                async with self._client.stream(
                    "GET", self._url(source, digest), headers=self._headers(source)
                ) as response:
                    if response.status_code != 200:
                        raise _status_error(
                            response.status_code, f"blob {digest}", source.name
                        )
                    async for piece in response.aiter_raw(_STREAM):
                        self._count(source, len(piece))
                        writer.write(piece)
                async with blob.lock:
                    if unit.done:
                        return
                    await asyncio.to_thread(writer.commit)
                    await self._won(source, unit)
        except CasIntegrityError:
            raise _Corrupt from None
        except CasError as exc:
            # A stream longer/shorter than the manifest says is the source's
            # fault (like a corrupt body); a full disk is ours.
            if exc.failure_type is FailureType.DISK_FULL:
                raise FetchError(
                    exc.message,
                    failure_type=exc.failure_type,
                    details={"source": source.name},
                ) from None
            raise FetchError(
                f"blob {digest}: {exc.message}", details={"source": source.name}
            ) from None
        except httpx.TimeoutException:
            raise FetchError(
                f"timed out fetching blob {digest}",
                failure_type=FailureType.TIMEOUT,
                details={"source": source.name},
            ) from None
        except httpx.HTTPError as exc:
            raise FetchError(
                f"blob fetch failed: {type(exc).__name__}",
                details={"source": source.name},
            ) from None

    async def _range(
        self, source: _Source, digest: Digest, start: int, length: int
    ) -> bytes:
        headers = self._headers(
            source, {"range": f"bytes={start}-{start + length - 1}"}
        )
        try:
            async with self._client.stream(
                "GET", self._url(source, digest), headers=headers
            ) as response:
                if response.status_code != 206:
                    raise _status_error(
                        response.status_code, f"blob {digest}", source.name
                    )
                data = bytearray()
                async for piece in response.aiter_raw(_STREAM):
                    data += piece
                    self._count(source, len(piece))
                    if len(data) > length:
                        raise FetchError(
                            "a range answer is longer than asked for",
                            details={"source": source.name},
                        )
        except httpx.TimeoutException:
            raise FetchError(
                f"timed out fetching blob {digest}",
                failure_type=FailureType.TIMEOUT,
                details={"source": source.name},
            ) from None
        except httpx.HTTPError as exc:
            raise FetchError(
                f"chunk fetch failed: {type(exc).__name__}",
                details={"source": source.name},
            ) from None
        if len(data) != length:
            raise FetchError(
                "a range answer ended early", details={"source": source.name}
            )
        return bytes(data)


async def _settle(units: list[_Unit]) -> None:
    pending = [f for u in units for f in u.attempts.values() if not f.done()]
    if pending:
        await asyncio.wait(pending)


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            return _first_leaf(exc)
        if isinstance(exc, FetchError):
            return exc
    leaf = group.exceptions[0]
    return _first_leaf(leaf) if isinstance(leaf, BaseExceptionGroup) else leaf
