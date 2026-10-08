"""Work-stealing transfer of blobs across sources (V0.5 G1 D80, G4 D89).

**Units of work.** Every blob that the cache does not hold becomes work units: a
**one-chunk** file (``size <= chunk_size``) is one unit - a ``GET`` streamed into
a :class:`~meshforge.common.cas.BlobWriter` that hashes as it writes and installs
the blob only if the digest matches; a **chunked** file is one unit per *missing*
chunk of its :class:`~meshforge.common.cas.ResumablePart` (``Range``). Every chunk
is verified against the manifest's chunk digest before it is written, the bitmap
sidecar records it, and the assembled file is hashed whole before it becomes a
blob (checklist 15). A crash or a cancellation keeps the verified chunks.

**Work stealing.** Each source runs ``per_source`` workers that take the next
unit from one shared queue, so a faster source simply takes more units. When the
queue is empty an idle worker *duplicates* a unit that has been in flight on
another source for longer than ``endgame_after_s`` (end-game mode); the first
verified answer wins and the other request is cancelled.

**Failure rules** are listed in :mod:`meshforge.agent.cas.fetcher`.
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from meshforge.agent.cas.sources import (
    CONTROLLER,
    STREAM,
    Corrupt,
    FetchError,
    FetchStats,
    FetchTuning,
    Source,
    status_error,
)
from meshforge.agent.cas.store import AgentCas
from meshforge.common.cas import (
    CasError,
    CasIntegrityError,
    ResumablePart,
    chunk_span,
)
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.tree import TreeEntry

__all__ = ["RangeFn", "TransferScheduler", "first_leaf"]

log = get_logger(__name__)

RangeFn = Callable[[Source, Digest, int, int], Awaitable[bytes]]
"""``(source, digest, start, length) -> exactly length verified-length bytes``."""


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


class TransferScheduler:
    """The per-staging transfer state: sources, the unit queue and the workers.

    One transfer at a time (:meth:`run`); the sources and their drops persist
    across transfers of one staging (a dropped peer stays dropped).
    """

    def __init__(
        self,
        cas: AgentCas,
        client: httpx.AsyncClient,
        sources: list[Source],
        stats: FetchStats,
        *,
        parallel: int,
        chunk_retries: int,
        max_refetches: int,
        tuning: FetchTuning,
        clock: Callable[[], float],
        get_range: RangeFn,
    ) -> None:
        self._cas = cas
        self._client = client
        self.sources = sources
        self.stats = stats
        self._parallel = parallel
        self._retries = chunk_retries
        self._tuning = tuning
        self._clock = clock
        self._range = get_range
        self._refetch_left = max_refetches
        self._refetched: set[str] = set()
        self._queue: deque[_Unit] = deque()
        self._inflight: list[_Unit] = []
        self._changed = asyncio.Condition()
        self._running = False

    # ---------------------------------------------------------- the sources

    def active(self) -> list[Source]:
        return [s for s in self.sources if s.dropped is None]

    def drop(self, source: Source, error: FetchError) -> None:
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

    def count(self, source: Source, n: int) -> None:
        self.stats.bytes_fetched += n
        source.stats.bytes += n

    def allow_refetch(self, digest: Digest) -> bool:
        """Spend the refetch budget: each blob at most once, at most
        ``max_refetches`` per staging (D3 of the G1 fix round)."""
        if digest.hex in self._refetched or self._refetch_left <= 0:
            return False
        self._refetched.add(digest.hex)
        self._refetch_left -= 1
        self.stats.refetches += 1
        return True

    def final(self, errors: list[FetchError]) -> FetchError:
        """The typed error of a unit nobody can serve (fetcher module doc)."""
        for error in errors:
            if error.source == CONTROLLER:
                return error
        if errors:
            last = errors[-1]
            return FetchError(
                f"no blob source could serve it (last: {last.message})",
                failure_type=FailureType.TRANSFER_ERROR,
            )
        return FetchError("no blob source left")

    # ---------------------------------------------------------------- blobs

    async def run(self, entries: list[TreeEntry], chunk_size: int) -> None:
        if not entries:
            return
        if self._running:
            raise RuntimeError("one transfer at a time per fetcher")
        self._running = True
        slots = asyncio.Semaphore(
            max(self._parallel, self._tuning.per_source * len(self.sources) * 2)
        )
        try:
            async with asyncio.TaskGroup() as group:
                blobs = [
                    group.create_task(self._blob(entry, chunk_size, slots))
                    for entry in entries
                ]
                workers = [
                    group.create_task(self._worker(source))
                    for source in self.sources
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
            raise first_leaf(group_error) from None
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
                self.stats.bytes_hit += entry.size
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

    async def _worker(self, source: Source) -> None:
        while source.dropped is None:
            unit = await self._take(source)
            if unit is None:
                return
            await self._attempt(source, unit)

    async def _take(self, source: Source) -> _Unit | None:
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

    def _steal(self, source: Source) -> _Unit | None:
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

    async def _attempt(self, source: Source, unit: _Unit) -> None:
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
        except Corrupt:
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

    def _failed(self, source: Source, unit: _Unit, exc: FetchError) -> None:
        if exc.failure_type is FailureType.NOT_FOUND:
            unit.blob.excluded[source.name] = exc  # this holder lost the blob
            return
        if source.controller and exc.failure_type in (
            FailureType.INVALID_INPUT,
            FailureType.DISK_FULL,
        ):
            unit.blob.excluded[source.name] = exc
            return
        self.drop(source, exc)

    def _corrupt(self, source: Source, unit: _Unit) -> FetchError:
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
            self.drop(source, error)  # a peer that serves bad bytes is out
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
        if any(not unit.barred(s.name) for s in self.active()):
            return
        reasons = [
            unit.excluded.get(s.name) or unit.blob.excluded.get(s.name) or s.dropped
            for s in self.sources
        ]
        unit.blob.finished.set_exception(
            self.final([r for r in reasons if r is not None])
        )

    def _check_slow(self) -> None:
        """Drop a peer slower than ``slow_source_factor`` x the median rate."""
        rated = [
            (s, s.stats.bytes / s.stats.duration_s)
            for s in self.active()
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
                self.drop(
                    source,
                    FetchError(
                        f"the source is too slow ({rate:.0f} B/s vs median {median:.0f})",
                        details={"source": source.name},
                    ),
                )

    # -------------------------------------------------------------- requests

    async def _request(self, source: Source, unit: _Unit) -> None:
        started = self._clock()
        try:
            if unit.index is None:
                await self._fetch_whole(source, unit)
            else:
                await self._fetch_chunk(source, unit)
        finally:
            source.stats.duration_s += self._clock() - started

    async def _won(self, source: Source, unit: _Unit) -> None:
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

    async def _fetch_chunk(self, source: Source, unit: _Unit) -> None:
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
                raise Corrupt from None
            except CasError as exc:
                raise FetchError(
                    exc.message,
                    failure_type=exc.failure_type,
                    details={"source": source.name},
                ) from None
            self.stats.chunks_fetched += 1
            await self._won(source, unit)

    async def _fetch_whole(self, source: Source, unit: _Unit) -> None:
        blob = unit.blob
        digest = blob.entry.digest
        store = self._cas.store
        try:
            with store.writer(digest, blob.entry.size) as writer:
                async with self._client.stream(
                    "GET",
                    source.url(digest),
                    headers=source.headers(),
                    extensions=source.extensions(),
                ) as response:
                    if response.status_code != 200:
                        raise status_error(
                            response.status_code, f"blob {digest}", source.name
                        )
                    async for piece in response.aiter_raw(STREAM):
                        self.count(source, len(piece))
                        writer.write(piece)
                async with blob.lock:
                    if unit.done:
                        return
                    await asyncio.to_thread(writer.commit)
                    await self._won(source, unit)
        except CasIntegrityError:
            raise Corrupt from None
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


async def _settle(units: list[_Unit]) -> None:
    pending = [f for u in units for f in u.attempts.values() if not f.done()]
    if pending:
        await asyncio.wait(pending)


def first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            return first_leaf(exc)
        if isinstance(exc, FetchError):
            return exc
    leaf = group.exceptions[0]
    return first_leaf(leaf) if isinstance(leaf, BaseExceptionGroup) else leaf
