"""GC and scrub of the controller's CAS (V0.5 G5, D91 + the G1 reviewer's scrub).

**GC.** The same LRU-under-quota pass as the agents (one shared index module,
:mod:`meshforge.common.gc_index`, at ``<data dir>/controller/gc/gc.sqlite``), with the
controller's own protection: before every pass the pins are recomputed from
PostgreSQL - the tree, bundle, split item list (V0.10), committed chunk
results and parts index (V0.10 G4), outputs and final logs of every
**non-terminal** job, the input trees of those jobs, and every
**pinned named input**, and (V0.15 G3) the artefacts of the active, pinned and
newest ``ml_keep_superseded`` superseded ML model sets. A tree pin protects
its manifest and every member
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
from meshforge.controller.ml.registry import protected_artefacts
from meshforge.controller.split.lanes import QUEUE_KIND as SPLIT_QUEUE_KIND
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.data import JobInput, NamedInput
from meshforge.controller.store.models.job import Attempt, Job
from meshforge.controller.store.models.ml import MlModel
from meshforge.workqueue.tables import work_item, work_queue

__all__ = ["BlobScrubber", "ControllerGc", "GcPassReport", "Protected", "SweepReport"]

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


@dataclass(slots=True)
class SweepReport:
    """V1.0 G11 (D454): one sweep of unreferenced blobs."""

    reason: str
    dry_run: bool = False
    removed: int = 0
    """Blobs unlinked (``dry_run``: that would be unlinked)."""
    removed_bytes: int = 0
    kept_recent: int = 0
    """Unreferenced blobs used within ``sweep_min_age_s`` (an upload, a
    ``have`` answer or a download in flight may still need them)."""
    kept_recent_bytes: int = 0
    failed: int = 0
    """Blobs whose unlink failed (logged; the next sweep retries)."""
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "removed_blobs": self.removed,
            "removed_bytes": self.removed_bytes,
            "kept_recent_blobs": self.kept_recent,
            "kept_recent_bytes": self.kept_recent_bytes,
            "failed_blobs": self.failed,
            "duration_s": round(self.duration_s, 3),
        }


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
        ml_keep_superseded: int = 5,
        sweep_min_age_s: float = 600.0,
    ) -> None:
        self.blobs = blobs
        self.sweep_min_age_s = sweep_min_age_s
        """V1.0 G11 (D454): an unreferenced blob used within this many
        seconds survives a sweep (in-flight upload / have / download)."""
        self.ml_keep_superseded = ml_keep_superseded
        """V0.15 G3: superseded model sets whose artefacts stay (rollback)."""
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
        return await self._references(every=False)

    async def referenced(self) -> Protected:
        """V1.0 G11 (D454): what any row still names - a superset of
        :meth:`protected`: the tree, bundle, split lists, chunk results,
        outputs and final logs of **every** job (finished ones too: ``compute
        get`` / ``compute logs`` read them), **every** named input (pinned or
        not) and every stored ML artefact. Only a finished job's named-input
        rows (``job_input``) are history, not a reference: the input's own
        row owns its tree, so ``compute data rm`` can free it."""
        return await self._references(every=True)

    async def _references(self, *, every: bool) -> Protected:
        out = Protected()
        async with self._sessions() as db:
            live_jobs = select(Job.id).where(Job.state.not_in(_TERMINAL))
            jobs = select(
                Job.tree_id,
                Job.bundle_digest,
                Job.split_items_digest,
                Job.split_parts_digest,
            )
            if not every:
                jobs = jobs.where(Job.state.not_in(_TERMINAL))
            rows = await db.execute(jobs)
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
            chunk_query = (
                select(work_item.c.result_digest)
                .join(work_queue, work_queue.c.id == work_item.c.queue_id)
                .where(
                    work_queue.c.kind == SPLIT_QUEUE_KIND,
                    work_item.c.result_digest.is_not(None),
                )
            )
            if not every:
                chunk_query = chunk_query.where(work_queue.c.owner_id.in_(live_jobs))
            chunks = await db.execute(chunk_query)
            out.blobs.update(h for (d,) in chunks.all() if (h := _hex(d)) is not None)
            # a finished job's inputs are history (D454): only live jobs pin them
            inputs = await db.execute(
                select(JobInput.tree_id).where(JobInput.job_id.in_(live_jobs))
            )
            out.trees.update(h for (t,) in inputs.all() if (h := _hex(t)) is not None)
            named = select(NamedInput.tree_id)
            if not every:
                named = named.where(NamedInput.pinned.is_(True))
            pinned = await db.execute(named)
            out.trees.update(h for (t,) in pinned.all() if (h := _hex(t)) is not None)
            outputs = select(Attempt.output_digest, Attempt.logs)
            if not every:
                outputs = outputs.where(Attempt.job_id.in_(live_jobs))
            attempts = await db.execute(outputs)
            for output, logs in attempts.all():
                if (h := _hex(output)) is not None:
                    out.blobs.add(h)
                for entry in (logs or {}).values():
                    if (
                        isinstance(entry, dict)
                        and (h := _hex(entry.get("digest"))) is not None
                    ):
                        out.blobs.add(h)
            # V0.15 G3: the ML model artefacts that may still be loaded
            out.blobs.update(await protected_artefacts(db, self.ml_keep_superseded))
            if every:
                artefacts = await db.scalars(
                    select(MlModel.artefact_digest).where(
                        MlModel.artefact_digest.is_not(None)
                    )
                )
                out.blobs.update(
                    h for d in artefacts.all() if (h := _hex(d)) is not None
                )
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

    async def sweep(
        self,
        reason: str,
        *,
        dry_run: bool = False,
        among: Iterable[str] | None = None,
    ) -> SweepReport:
        """V1.0 G11 (D454): unlink every stored blob no row references
        (:meth:`referenced`) that was not used within ``sweep_min_age_s`` -
        now, not at quota pressure. ``among`` limits it to these blob hexes
        (``compute data rm``: the removed tree's manifest and members).

        Holds :attr:`BlobStore.gc_guard` exclusively for the whole sweep, like
        :meth:`run_pass`: a job submission or a named-input push (which hold
        it shared from their tree check to their row) can never commit a new
        reference to a blob this sweep removes - it either committed before
        (and is read here) or runs after (and finds the blob missing). The
        references are read inside the guard; files go only after that."""
        started = self._clock()
        report = SweepReport(reason, dry_run=dry_run)
        only = None if among is None else set(among)
        async with self.blobs.gc_guard.exclusive():
            refs = await self.referenced()
            keep = set(refs.blobs) | set(refs.trees)
            members, known = await asyncio.to_thread(self.index.members_of, refs.trees)
            keep |= members
            for tree_hex in refs.trees - known:
                keep |= await self.blobs.tree_members(
                    Digest.parse(f"sha256:{tree_hex}")
                )
            cutoff = self._wall() - self.sweep_min_age_s
            stored = await asyncio.to_thread(self.index.items, "blob")
            victims: list[Victim] = []
            for key, size, last_use in stored:
                if key in keep or (only is not None and key not in only):
                    continue
                if last_use > cutoff:
                    report.kept_recent += 1
                    report.kept_recent_bytes += size
                    continue
                victims.append(Victim("blob", key, size))
            if dry_run:
                report.removed = len(victims)
                report.removed_bytes = sum(v.size for v in victims)
            else:
                await asyncio.to_thread(self._sweep_evict, victims, report)
        report.duration_s = self._clock() - started
        log.info(
            "controller.gc.sweep",
            reason=reason,
            dry_run=dry_run,
            removed=report.removed,
            removed_bytes=report.removed_bytes,
            kept_recent=report.kept_recent,
            failed=report.failed,
            scoped=only is not None,
            duration_s=round(report.duration_s, 3),
        )
        return report

    def _sweep_evict(self, victims: list[Victim], report: SweepReport) -> None:
        for i in range(0, len(victims), self._batch):
            batch = victims[i : i + self._batch]
            with self.index.tx():
                # a lease or pin taken since the listing still wins
                held = self.index.protected_blobs(v.key for v in batch)
                for victim in batch:
                    if victim.key in held:
                        continue
                    if self._evict(victim):
                        report.removed += 1
                        report.removed_bytes += victim.size
                    else:
                        report.failed += 1

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
