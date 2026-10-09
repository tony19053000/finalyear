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

Layout: :mod:`.sources` (source identity, stats, typed errors, the ``Range``
GET), :mod:`.manifest` (the manifest GET), :mod:`.scheduler` (units, queue,
work-stealing workers, failure rules) and this facade.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from typing import Final

import httpx

from meshforge.agent.cas.manifest import get_manifest, load_tree
from meshforge.agent.cas.scheduler import TransferScheduler
from meshforge.agent.cas.sources import (
    BlobSource,
    FetchError,
    FetchStats,
    FetchTuning,
    Source,
    SourceStats,
    TokenLookup,
    get_range,
)
from meshforge.agent.cas.store import AgentCas
from meshforge.common.cas import CasError
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.tree import Tree, TreeEntry, TreeError, TreeLimits

__all__ = [
    "BlobFetcher",
    "BlobSource",
    "FetchError",
    "FetchStats",
    "FetchTuning",
    "SourceStats",
]

DEFAULT_PARALLEL: Final = 4
DEFAULT_CHUNK_RETRIES: Final = 2
DEFAULT_MAX_REFETCHES: Final = 3
"""Whole blobs one staging may fetch again after a mismatch (each blob once)."""


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
        token_for: TokenLookup | None = None,
    ) -> None:
        """``token_for`` (V0.7 G3, D147): asked before every request to a peer
        for the newest token the attempt holds for it (peer tokens live
        <= 60 s and are re-minted while the attempt stages)."""
        listed = list(sources)
        if not listed:
            raise ValueError("at least one blob source is needed")
        if parallel < 1 or chunk_retries < 0:
            raise ValueError("parallel >= 1 and chunk_retries >= 0")
        self._cas = cas
        self._client = client
        self._limits = tree_limits or TreeLimits()
        tuning = tuning or FetchTuning()
        if tuning.per_source < 1:
            raise ValueError("per_source >= 1")
        self._clock = clock
        self.stats = FetchStats()
        known: list[Source] = []
        seen: set[str] = set()
        for src in listed:
            if src.name in seen:
                continue  # one entry per source (a duplicate listing adds nothing)
            seen.add(src.name)
            stats = self.stats.sources.setdefault(src.name, SourceStats())
            known.append(Source(src, stats, token_for=token_for))
        self._sched = TransferScheduler(
            cas,
            client,
            known,
            self.stats,
            parallel=parallel,
            chunk_retries=chunk_retries,
            max_refetches=max_refetches,
            tuning=tuning,
            clock=clock,
            # Late-bound: resolved per call so ``_range`` stays one seam.
            get_range=lambda *args: self._range(*args),
        )

    @property
    def sources(self) -> list[BlobSource]:
        return [s.src for s in self._sched.sources]

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
                    tree = await load_tree(data, tree_id, self._limits)
                except TreeError:
                    # A cached manifest that no longer verifies: evict, refetch.
                    await asyncio.to_thread(store.evict, tree_id)
                else:
                    self.stats.manifest_hit = True
                    return tree
            errors: list[FetchError] = []
            for source in self._sched.active():
                try:
                    data = await get_manifest(
                        self._client,
                        source,
                        tree_id,
                        max_bytes=self._limits.max_manifest_bytes,
                        count=self._sched.count,
                        clock=self._clock,
                    )
                except FetchError as exc:
                    errors.append(exc)
                    if not source.controller:
                        self._sched.drop(source, exc)
                        continue
                    if exc.failure_type in (
                        FailureType.INTEGRITY_ERROR,
                        FailureType.INVALID_INPUT,
                    ):
                        raise  # the controller sent something wrong: not a retry
                    continue  # unauthorized / unreachable: a peer may still help
                tree = await load_tree(data, tree_id, self._limits)
                try:
                    await asyncio.to_thread(store.put_bytes, data, tree_id)
                except CasError as exc:
                    raise FetchError(
                        exc.message, failure_type=exc.failure_type
                    ) from None
                return tree
            raise self._sched.final(errors)

    # ---------------------------------------------------------------- blobs

    async def ensure_all(self, tree: Tree) -> FetchStats:
        """Every file blob of ``tree`` is in the cache (fetched if missing)."""
        unique: dict[str, TreeEntry] = {}
        for entry in tree.files():
            unique.setdefault(entry.sha256, entry)
        await self._sched.run(list(unique.values()), tree.chunk_size)
        return self.stats

    async def ensure(self, entry: TreeEntry, chunk_size: int) -> bool:
        """Fetch ``entry``'s blob unless cached; True = it was fetched now."""
        before = self.stats.blobs_fetched
        await self._sched.run([entry], chunk_size)
        return self.stats.blobs_fetched > before

    def allow_refetch(self, digest: Digest) -> bool:
        """Spend the refetch budget: each blob at most once, at most
        ``max_refetches`` per staging (D3 of the G1 fix round)."""
        return self._sched.allow_refetch(digest)

    async def _range(
        self, source: Source, digest: Digest, start: int, length: int
    ) -> bytes:
        return await get_range(
            self._client, source, digest, start, length, self._sched.count
        )
