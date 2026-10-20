"""GC and scrub of the controller's CAS (V0.5 G5, D91 + the G1 reviewer's scrub).

**GC.** The same LRU-under-quota pass as the agents (one shared index module,
:mod:`meshforge.common.gc_index`, at ``<data dir>/controller/gc/gc.sqlite``), with the
controller's own protection: before every pass the pins are recomputed from
PostgreSQL - the tree, bundle, split item list (V0.10), committed chunk
results and parts index (V0.10 G4), outputs and final logs of every
**non-terminal** job, the input trees of those jobs, and every
**pinned named input**. A tree pin protects its manifest and every member
(members recorded from the stored manifest). Blobs used within ``min_age_s``
(an upload, a ``have`` answer, a download) are never evicted, which covers
the window between a client's
have/need and its submit. The pass holds :attr:`BlobStore.gc_guard`
exclusively; a job submission (tree check -> job row) and a named-input push
hold it shared, so a job can never be created on a blob a running pass is about
to remove, and submissions still run concurrently with each other.

**Scrub.** Stored blobs can rot (disk errors, a hand edit). The scrubber
re-hashes blobs at a bounded rate (``scrub_bytes_per_s``) in a slow cycle
(``scrub_interval_s`` between full cycles) and **first** whatever is suspected:
the blobs of a tree an agent failed with ``integrity_error`` (a corrupt answer
from the controller), and a stored manifest that does not hash to its name. A
blob that does not match its name is evicted (logged
``controller.blobs.scrub_corrupt``) - never served again; ``have`` then reports
it missing, so the next upload replaces it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.cas import CasStore
from meshforge.common.gc_index import GcIndex, Victim, collect_kind
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.states import JOB_TERMINAL_STATES
from meshforge.controller.blobs import BlobStore
from meshforge.controller.split.lanes import QUEUE_KIND as SPLIT_QUEUE_KIND
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.data import JobInput, NamedInput
from meshforge.controller.store.models.job import Attempt, Job
from meshforge.workqueue.tables import work_item, work_queue

__all__ = ["BlobScrubber", "ControllerGc", "GcPassReport", "Protected"]

log = get_logger(__name__)

_TERMINAL: Final = tuple(s.value for s in JOB_TERMINAL_STATES)
_SUSPECT_MAX: Final = 100_000


@dataclass(slots=True)
class Protected:
    trees: set[str] = field(default_factory=set)
    """Hex of trees whose manifest + members must stay."""
    blobs: set[str] = field(default_factory=set)
    """Hex of single blobs that must stay (bundles, outputs, logs)."""


@dataclass(slots=True)
class GcPassReport:
    reason: str
    evicted: int = 0
    evicted_bytes: int = 0
    usage: int = 0
    over_quota: bool = False
    protected_trees: int = 0
    protected_blobs: int = 0
    duration_s: float = 0.0


def _hex(digest: str | None) -> str | None:
    if digest and digest.startswith("sha256:") and len(digest) == 71:
        return digest[7:]
    return None


class ControllerGc:
    """See module docstring (GC part)."""

    def __init__(
        self,
        blobs: BlobStore,
        engine: AsyncEngine,
        *,
        index_dir: Path,
        quota_bytes: int,
        min_age_s: float,
        interval_s: float,
        batch: int = 256,
        wall: Callable[[], float] = time.time,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.blobs = blobs
        self.index = GcIndex(index_dir, wall=wall)
        self._sessions = create_session_factory(engine)
        self.quota_bytes = quota_bytes
        self.min_age_s = min_age_s
        self._interval_s = interval_s
        self._batch = batch
        self._wall = wall
        self._clock = clock
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.passes = 0
        self.last: GcPassReport | None = None

    @property
    def store(self) -> CasStore:
        return self.blobs.cas

    async def start(self) -> None:
        await asyncio.to_thread(self.index.open)
        self.store.on_change = self._on_change
        await asyncio.to_thread(self._reconcile)
        self._task = asyncio.create_task(self._run(), name="controller-gc")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.store.on_change = None
        await asyncio.to_thread(self.index.close)

    def _on_change(self, added: bool, digest: Digest, size: int) -> None:
        try:
            if added:
                self.index.upsert("blob", digest.hex, size)
            else:
                self.index.remove("blob", digest.hex)
        except RuntimeError:
            pass  # closed: the next start reconciles with the disk

    def _reconcile(self) -> None:
        self.index.reconcile("blob", self.store.listing())

    async def touch(self, digests: Iterable[Digest]) -> None:
        """These blobs were just used (a ``have`` answer, a download)."""
        hexes = [d.hex for d in digests]
        if hexes:
            with contextlib.suppress(RuntimeError):
                await asyncio.to_thread(self.index.touch, "blob", hexes)

    def request(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval_s)
            self._wake.clear()
            try:
                await self.run_pass("periodic")
            except Exception as exc:  # noqa: BLE001 - retried at the next interval
                log.warning(
                    "controller.gc.pass_failed",
                    cause=type(exc).__name__,
                    error=str(exc)[:200],
                )

    async def protected(self) -> Protected:
        """What PostgreSQL says must stay (module docstring)."""
        out = Protected()
        async with self._sessions() as db:
            live_jobs = select(Job.id).where(Job.state.not_in(_TERMINAL))
            rows = await db.execute(
                select(
                    Job.tree_id,
                    Job.bundle_digest,
                    Job.split_items_digest,
                    Job.split_parts_digest,
                ).where(Job.state.not_in(_TERMINAL))
            )
            for tree_id, bundle, split_items, split_parts in rows.all():
                if (h := _hex(tree_id)) is not None:
                    out.trees.add(h)
                if (h := _hex(bundle)) is not None:
                    out.blobs.add(h)
                if (h := _hex(split_items)) is not None:  # V0.10 G1 item list
                    out.blobs.add(h)
                if (h := _hex(split_parts)) is not None:  # V0.10 G4 parts index
                    out.blobs.add(h)
            # V0.10 G4 (D261): the committed chunk results of live split jobs
            chunks = await db.execute(
                select(work_item.c.result_digest)
                .join(work_queue, work_queue.c.id == work_item.c.queue_id)
                .where(
                    work_queue.c.kind == SPLIT_QUEUE_KIND,
                    work_queue.c.owner_id.in_(live_jobs),
                    work_item.c.result_digest.is_not(None),
                )
            )
            out.blobs.update(h for (d,) in chunks.all() if (h := _hex(d)) is not None)
            inputs = await db.execute(
                select(JobInput.tree_id).where(JobInput.job_id.in_(live_jobs))
            )
            out.trees.update(h for (t,) in inputs.all() if (h := _hex(t)) is not None)
            pinned = await db.execute(
                select(NamedInput.tree_id).where(NamedInput.pinned.is_(True))
            )
            out.trees.update(h for (t,) in pinned.all() if (h := _hex(t)) is not None)
            attempts = await db.execute(
                select(Attempt.output_digest, Attempt.logs).where(
                    Attempt.job_id.in_(live_jobs)
                )
            )
            for output, logs in attempts.all():
                if (h := _hex(output)) is not None:
                    out.blobs.add(h)
                for entry in (logs or {}).values():
                    if (
                        isinstance(entry, dict)
                        and (h := _hex(entry.get("digest"))) is not None
                    ):
                        out.blobs.add(h)
        return out

    async def run_pass(self, reason: str = "manual") -> GcPassReport:
        started = self._clock()
        report = GcPassReport(reason)
        async with self.blobs.gc_guard.exclusive():
            protected = await self.protected()
            for tree_hex in protected.trees:
                if not await asyncio.to_thread(self.index.tree_known, tree_hex):
                    members = await self.blobs.tree_members(
                        Digest.parse(f"sha256:{tree_hex}")
                    )
                    await asyncio.to_thread(self.index.record_tree, tree_hex, members)
            await asyncio.to_thread(self.index.set_pins, "tree", protected.trees)
            await asyncio.to_thread(self.index.set_pins, "blob", protected.blobs)
            result = await asyncio.to_thread(
                functools.partial(
                    collect_kind,
                    self.index,
                    "blob",
                    self.quota_bytes,
                    cutoff=lambda: self._wall() - self.min_age_s,
                    batch=self._batch,
                    evict=self._evict,
                )
            )
        report.evicted = result.evicted
        report.evicted_bytes = result.evicted_bytes
        report.usage = result.usage
        report.over_quota = result.over_quota
        report.protected_trees = len(protected.trees)
        report.protected_blobs = len(protected.blobs)
        report.duration_s = self._clock() - started
        self.passes += 1
        self.last = report
        log.info(
            "controller.gc.pass",
            reason=reason,
            evicted=report.evicted,
            evicted_bytes=report.evicted_bytes,
            usage=report.usage,
            over_quota=report.over_quota,
            protected_trees=report.protected_trees,
            protected_blobs=report.protected_blobs,
            duration_s=round(report.duration_s, 3),
        )
        return report

    def _evict(self, victim: Victim) -> bool:
        digest = Digest.parse(f"sha256:{victim.key}")
        try:
            self.store.evict(digest)
        except OSError as exc:
            log.warning(
                "controller.gc.evict_failed",
                digest=str(digest),
                cause=type(exc).__name__,
            )
            return False
        self.index.remove("blob", victim.key)
        self.blobs.forget_tree(digest)
        return True


class BlobScrubber:
    """See module docstring (scrub part)."""

    def __init__(
        self,
        blobs: BlobStore,
        *,
        bytes_per_s: int,
        interval_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if bytes_per_s < 1:
            raise ValueError("bytes_per_s >= 1")
        self.blobs = blobs
        self._rate = bytes_per_s
        self._interval_s = interval_s
        self._clock = clock
        self._suspects: deque[str] = deque()
        self._trees: deque[tuple[Digest, str]] = deque()
        self._queued: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.checked = 0
        self.corrupt = 0
        self.cycles = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="controller-scrub")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def suspect(self, digests: Iterable[Digest], *, why: str) -> int:
        """Verify these first (bounded queue; duplicates ignored)."""
        added = 0
        for digest in digests:
            if digest.hex in self._queued or len(self._suspects) >= _SUSPECT_MAX:
                continue
            self._queued.add(digest.hex)
            self._suspects.append(digest.hex)
            added += 1
        if added:
            log.info("controller.blobs.scrub_suspects", count=added, why=why)
            self._wake.set()
        return added

    async def suspect_tree(self, tree_id: Digest, *, why: str) -> int:
        members = await self.blobs.tree_members(tree_id)
        return self.suspect(
            [tree_id, *(Digest.parse(f"sha256:{h}") for h in sorted(members))],
            why=why,
        )

    def suspect_tree_soon(self, tree_id: str, *, why: str) -> None:
        """Synchronous form for report handlers: the scrub task expands the
        tree's members and verifies them first."""
        try:
            digest = Digest.parse(tree_id)
        except ValueError:
            return
        if len(self._trees) < _SUSPECT_MAX:
            self._trees.append((digest, why))
            self._wake.set()

    async def check(self, digest: Digest) -> bool | None:
        """Re-hash one stored blob; a mismatch evicts it. True = good, False =
        corrupt (now removed), None = not stored."""
        if self.blobs.size(digest) is None:
            return None
        good = await asyncio.to_thread(self.blobs.cas.verify, digest)
        self.checked += 1
        if not good:
            self.corrupt += 1
            self.blobs.forget_tree(digest)
            log.error(
                "controller.blobs.scrub_corrupt",
                digest=str(digest),
                detail="the stored blob does not match its name; removed, never served",
            )
        return good

    async def _pace(self, size: int) -> None:
        await asyncio.sleep(size / self._rate)

    async def _drain_suspects(self) -> None:
        while self._trees:
            tree_id, why = self._trees.popleft()
            await self.suspect_tree(tree_id, why=why)
        while self._suspects:
            hex_ = self._suspects.popleft()
            self._queued.discard(hex_)
            digest = Digest.parse(f"sha256:{hex_}")
            size = self.blobs.size(digest) or 0
            await self.check(digest)
            await self._pace(size)

    async def run_cycle(self) -> None:
        """One full pass over the store (suspects first, between every blob)."""
        listing = await asyncio.to_thread(self.blobs.cas.listing)
        for hex_, size in sorted(listing.items()):
            await self._drain_suspects()
            await self.check(Digest.parse(f"sha256:{hex_}"))
            await self._pace(size)
        self.cycles += 1

    async def _run(self) -> None:
        next_cycle = self._clock() + self._interval_s
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), max(0.0, next_cycle - self._clock())
                )
            self._wake.clear()
            try:
                await self._drain_suspects()
                if self._clock() >= next_cycle:
                    await self.run_cycle()
                    next_cycle = self._clock() + self._interval_s
            except Exception as exc:  # noqa: BLE001 - keep scrubbing
                log.warning("controller.blobs.scrub_failed", cause=type(exc).__name__)
