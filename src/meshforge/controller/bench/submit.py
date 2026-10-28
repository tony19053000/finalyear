"""Forced benchmark runs: one planned run -> one ordinary job (V0.9 G3, roadmap 0.9.4, D215).

A benchmark run is **an ordinary job** (ARCHITECTURE §17): it is planned,
staged, executed and recorded by exactly the paths a user's job takes. What
makes it "forced" is only how it is submitted:

* **pinned** to the run's node (``placement.node_id``), ``mode`` ``local``
  when the node is the origin and ``offload`` otherwise, with
  ``submitter_node_id`` = the origin - so the run record's ``strategy`` is
  ``local`` / ``mode1`` by the same rule as for any job;
* at the **lowest priority** (:data:`~meshforge.common.manifest.MIN_PRIORITY`):
  a user's job queued later is dispatched first (D215; benchmark jobs never
  pre-empt - the fabric has no pre-emption - and never jump the queue);
* owned by the campaign's creator (D141: they see, cancel and download it).

The planner still runs at dispatch (D215: no bypass) and, because the job is
pinned, also plans it unpinned as a shadow - the record's ``planner_node_id``
(:mod:`meshforge.controller.jobs.planning`).

**Same internal path as ``POST /v1/jobs``** (no HTTP self-call): the job folder
of :func:`meshforge.bench.workloads.materialise.job_files` is turned into a
tree manifest in memory (the same canonical form ``compute run`` builds from a
folder, :func:`~meshforge.common.tree.canonical_bytes`) and its blobs are put
in the blob store; under the store's GC guard the tree is checked like a
submitted one (:func:`~meshforge.controller.api.job_routes.check_tree` rules),
the pinned placement is checked (:func:`~meshforge.controller.jobs.placement.
check_placement`), and :meth:`~meshforge.controller.jobs.store.JobStore.
create_queued_job` writes the job. The same ``(family, params, seed)`` gives
byte-identical files (G2), so the blobs are stored once per group.

**Idempotent per run.** The job's ``Idempotency-Key`` is ``bench:<run_id>``:
a crash (or a racing submit) between the job's commit and the run's
``planned -> submitted`` link finds the same job again and links it - never a
second job for one run.

**Generations (V0.9 G5).** A run the campaign runner requeued (infra retry,
owner pre-emption) is submitted again under ``bench:<run_id>:<generation>``
(:func:`~.keys.run_key`): a new job per generation, never a second job for one
generation. Its fillers / warm-up get this generation's keys too.

**Owner guard (V0.9 G5, D246).** A node whose owner paused benchmarks or is
using it (``pause_benchmarks`` / the heartbeat's ``owner_active``) defers the
run node-wide - nothing is created there.

**Outcomes** (:class:`RunOutcome`): ``submitted`` (a job, linked);
``deferred`` (the node is offline / has no session / its owner's policy refuses
jobs now: the run stays ``planned`` and the next submit tries it again);
``skipped`` (the run can never run as planned: node deleted, cannot give the
job what it needs, the library refuses the params - the run becomes
``skipped`` with the reason); ``already`` (another submit moved it first).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.bench.workloads import ParamsError, UnknownFamilyError
from meshforge.bench.workloads.materialise import job_files
from meshforge.common.bench_conditions import (
    CacheCondition,
    Condition,
    ConditionError,
    QueueCondition,
    Unavailable,
    parse_condition,
)
from meshforge.common.cas import DEFAULT_CHUNK_SIZE
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_job_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import (
    MANIFEST_FILE_NAME,
    MIN_PRIORITY,
    Manifest,
    ManifestError,
    parse_manifest_data,
)
from meshforge.common.owner_policy import BENCH_PAUSED_REFUSAL, OWNER_ACTIVE_REFUSAL
from meshforge.common.split_items import SplitLimits
from meshforge.common.tree import Tree, TreeEntry, TreeError, canonical_bytes
from meshforge.controller.bench.conditions import NodePauses, filler_key, warmup_key
from meshforge.controller.bench.keys import run_key
from meshforge.controller.bench.store import (
    BENCH_KEY_PREFIX,
    BenchStore,
    CampaignRecord,
    RunRow,
)
from meshforge.controller.blobs import BlobError, BlobStore
from meshforge.controller.jobs.env_health import env_key
from meshforge.controller.jobs.placement import PlacementError, check_placement
from meshforge.controller.jobs.split_items import resolve_split
from meshforge.controller.jobs.store import (
    IdempotencyConflictError,
    JobRecord,
    JobScheduling,
    JobStore,
    SplitRecord,
)
from meshforge.controller.planner.inputs import workload_fingerprint
from meshforge.controller.planner.types import RejectCode
from meshforge.controller.registry import Registry
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.auth import AppUser

__all__ = [
    "IDEMPOTENCY_PREFIX",
    "BenchSubmitError",
    "BenchSubmitter",
    "RunOutcome",
    "bench_tree",
    "owner_guarded",
    "run_placement",
]

log = get_logger(__name__)

IDEMPOTENCY_PREFIX: Final = BENCH_KEY_PREFIX
_TRANSIENT_CODES_PREFIX: Final = "policy_"
"""Owner-policy refusals change when the owner changes their policy: deferred,
not skipped (G5's owner guard builds on this)."""

NODE_WIDE_DEFERRALS: Final = frozenset(
    {
        "",
        RejectCode.POLICY_NOT_ACCEPTING.value,
        RejectCode.POLICY_USER_NOT_ALLOWED.value,
        RejectCode.BENCH_PAUSED.value,
    }
)
"""Deferral reject codes that hold for every run on the node ("" = the node is
not online / has no live session): :meth:`BenchSubmitter.submit_next` passes
the node over for the rest of the call."""
MAX_EXAMINED: Final = 2_000
"""Most planned runs one :meth:`BenchSubmitter.submit_next` call examines
(each costs a job-folder build and a placement check): bounds one request
when most runs defer per run or are skipped."""
_PAGE: Final = 200

type Outcome = Literal["submitted", "deferred", "skipped", "already"]


class BenchSubmitError(MeshForgeError):
    """A submit could not proceed (a store or blob failure; retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    seq: int
    outcome: Outcome
    job_id: str | None = None
    detail: str | None = None
    node_wide: bool = False
    """``deferred`` for a reason of the node, not of this run
    (:data:`NODE_WIDE_DEFERRALS`): its other runs would defer too."""


@dataclass(frozen=True, slots=True)
class SubmitBatch:
    """What one :meth:`BenchSubmitter.submit_next` call did (fix r2, D251 N1)."""

    outcomes: tuple[RunOutcome, ...] = ()
    cursor: int = 0
    """Where the next call should start: 0 = the head; else the last ``seq``
    examined when the examine cap was hit before ``count`` were submitted."""
    stale: bool = False
    """A link failed: the batch stopped early (D250 D-1)."""

    @property
    def submitted(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "submitted")


@dataclass(frozen=True, slots=True)
class BuiltTree:
    tree: Tree
    manifest_bytes: bytes
    blobs: dict[str, bytes]
    """hex digest -> bytes (each file's content)."""
    manifest: Manifest


def bench_tree(family: str, params: dict[str, object], seed: int) -> BuiltTree:
    """The run's job folder as a tree, in memory (no disk). Raises
    :class:`~meshforge.bench.workloads.ParamsError` /
    :class:`~meshforge.bench.workloads.UnknownFamilyError` /
    :class:`~meshforge.common.manifest.ManifestError`."""
    files = job_files(family, params, int(seed))
    entries: list[TreeEntry] = []
    blobs: dict[str, bytes] = {}
    for path, data in files.items():
        if len(data) > DEFAULT_CHUNK_SIZE:  # pragma: no cover - kernels are KBs
            raise TreeError("a benchmark file is larger than one chunk", path=path)
        sha = hashlib.sha256(data).hexdigest()
        blobs[sha] = data
        entries.append(TreeEntry(path, "file", sha256=sha, size=len(data)))
    ordered = tuple(sorted(entries, key=lambda e: e.path.encode("utf-8")))
    body = canonical_bytes(DEFAULT_CHUNK_SIZE, ordered)
    tree = Tree(
        chunk_size=DEFAULT_CHUNK_SIZE,
        entries=ordered,
        tree_id=Digest(hashlib.sha256(body).hexdigest()),
        manifest_bytes=len(body),
    )
    raw = yaml.safe_load(files[MANIFEST_FILE_NAME])
    manifest = parse_manifest_data(raw)
    return BuiltTree(tree=tree, manifest_bytes=body, blobs=blobs, manifest=manifest)


def run_placement(run: RunRow) -> dict[str, Any]:
    """The job placement of a run: pinned to its node (``local`` /
    ``mode1``; for a split family that means "don't split" there) or - V0.10
    G8 - forced onto its node set (``mode3``: ``auto`` + ``node_set``, the
    planner splits over exactly those nodes)."""
    if run.node_set:
        return {"mode": "auto", "node_set": list(run.node_set)}
    return {
        "mode": "local" if run.node_id == run.origin_node_id else "offload",
        "node_id": run.node_id,
    }


def owner_guarded(live: object) -> str | None:
    """V0.9 G5 (D246): the live session says benchmarks may not run there."""
    if live is None:
        return None
    policy = getattr(live, "policy", None)
    if policy is not None and getattr(policy, "pause_benchmarks", False):
        return BENCH_PAUSED_REFUSAL.text
    if getattr(live, "owner_active", False):
        return OWNER_ACTIVE_REFUSAL.text
    return None


def _guard_deferral(run: RunRow, text: str) -> RunOutcome:
    log.info(
        "controller.bench.run_deferred",
        run_id=run.id,
        node_id=run.node_id,
        reason=text,
    )
    return RunOutcome(run.id, run.seq, "deferred", detail=text, node_wide=True)


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _pre_jobs(run_id: str, condition: Condition, generation: int = 0) -> list[str]:
    """Keys of the jobs a controller-side condition queues before the run
    (this generation's: a requeued run gets new fillers / a new warm-up)."""
    if isinstance(condition, QueueCondition):
        first = generation * condition.jobs + 1
        return [filler_key(run_id, i) for i in range(first, first + condition.jobs)]
    if isinstance(condition, CacheCondition) and condition.mode == "warm":
        return [warmup_key(run_id, generation)]
    return []


def _request_hash(
    run: RunRow,
    manifest: dict[str, object],
    tree_id: str,
    placement: dict[str, Any],
    *,
    key: str,
) -> str:
    extra = {} if key == IDEMPOTENCY_PREFIX + run.id else {"key": key}
    canonical = json.dumps(
        {
            **extra,
            "bench_run": run.id,
            "manifest": manifest,
            "tree_id": tree_id,
            "placement": placement,
            "submitter": run.origin_node_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BenchSubmitter:
    """Submits a campaign's planned runs (see module docstring)."""

    def __init__(
        self,
        *,
        store: BenchStore,
        jobs: JobStore,
        blobs: BlobStore,
        registry: Registry,
        manager: SessionManager,
        engine: AsyncEngine,
        wake: Callable[[], None] | None = None,
        pauses: NodePauses | None = None,
        split_limits: SplitLimits | None = None,
    ) -> None:
        self._split_limits = split_limits or SplitLimits()
        """V0.10 G8 (D278): a split family's items resolve under these caps."""
        self._pauses = pauses
        """V0.9 G4: where ``unavailable:T`` pauses a run's origin."""
        self._store = store
        self._jobs = jobs
        self._blobs = blobs
        self._registry = registry
        self._manager = manager
        self._sessions = create_session_factory(engine)
        self._wake = wake
        self.cancel_job: Callable[[str], Awaitable[object]] | None = None
        """Fix r1 (D250): cancels a job created for a run that moved on
        (the dispatcher's ``cancel``; set by the server)."""

    async def _owner_name(self, user_id: str | None) -> str | None:
        if user_id is None:
            return None
        async with self._sessions() as db:
            name = await db.scalar(select(AppUser.name).where(AppUser.id == user_id))
        return None if name is None else str(name)

    async def submit_next(
        self,
        campaign: CampaignRecord,
        count: int,
        *,
        node_budget: Mapping[str, int] | None = None,
        max_examined: int = MAX_EXAMINED,
        start_after: int = 0,
    ) -> SubmitBatch:
        """Submit up to ``count`` planned runs in ``seq`` order, starting after
        ``start_after`` and wrapping around to the head once. Raises
        :class:`BenchSubmitError` (nothing after the failing run is tried).

        **No head-of-line stall** (G4 item 0.5, D239.1): a *deferred* run does
        not use up ``count`` - the call keeps paging through the planned runs
        until ``count`` are submitted or the runs are exhausted. A node that
        deferred a run for a **node-wide** reason (offline, no live session,
        its owner accepts no jobs / not from this campaign's owner / the owner
        guard: :data:`NODE_WIDE_DEFERRALS`) is passed over for the rest of the
        call, in SQL. A deferral that depends on the job (an owner RAM / CPU
        limit...) is per run. At most ``max_examined`` (<=
        :data:`MAX_EXAMINED`) runs are examined per call; skipped and
        ``already`` runs are listed but do not count as submitted.

        V0.9 G5 (the runner): ``node_budget`` = how many runs each node may
        get in this call - a node not in it (or at 0) is passed over in SQL.
        ``start_after`` (fix r1, D249): the runner's cursor - a call that hit
        the examine cap returns the last ``seq`` it examined
        (:attr:`SubmitBatch.cursor`), so more per-run deferrals at the head
        than the cap cannot stall the campaign tick after tick.

        **A link that fails stops the batch** (fix r1, D250 D-1): the run moved
        on under us (a cancel, another submit) - this page is stale, and the
        job just created for it is cancelled unless the run is linked to it."""
        owner_name = await self._owner_name(campaign.created_by)
        outcomes: list[RunOutcome] = []
        budget = None if node_budget is None else dict(node_budget)
        passed_over: set[str] = set()
        if budget is not None:
            passed_over.update(n for n, k in budget.items() if k <= 0)
        max_examined = max(1, min(max_examined, MAX_EXAMINED))
        examined = submitted = 0
        last_seq = start_after
        stale = False
        # two passes: after the cursor to the end, then the head up to it
        passes: list[tuple[int, int | None]] = [(start_after, None)]
        if start_after > 0:
            passes.append((0, start_after))
        for after_seq, upto in passes:
            done_pass = False
            while not done_pass and submitted < count and examined < max_examined:
                if budget is not None and not any(
                    k > 0 for n, k in budget.items() if n not in passed_over
                ):
                    break
                limit = min(_PAGE, max_examined - examined)
                page = await self._store.runs(
                    campaign.id,
                    state="planned",
                    after_seq=after_seq,
                    limit=limit,
                    exclude_nodes=sorted(passed_over),
                    only_nodes=None
                    if budget is None
                    else sorted(
                        n for n, k in budget.items() if k > 0 and n not in passed_over
                    ),
                )
                if not page:
                    break
                for run in page:
                    if upto is not None and run.seq > upto:
                        done_pass = True
                        break
                    after_seq = run.seq
                    if run.node_id in passed_over:
                        continue  # deferred earlier on this page
                    if budget is not None and budget.get(run.node_id, 0) <= 0:
                        continue  # not a node of this call
                    if budget is not None and any(
                        budget.get(n, 0) <= 0 for n in run.node_set
                    ):
                        continue  # V0.10 G8: a mode3 run needs its whole set free
                    examined += 1
                    last_seq = run.seq
                    outcome = await self.submit_run(
                        campaign, run, owner_name=owner_name
                    )
                    outcomes.append(outcome)
                    if outcome.outcome == "submitted":
                        submitted += 1
                        if budget is not None:
                            # V0.10 G8: a mode3 run takes every node of its set
                            for used in run.node_set or (run.node_id,):
                                budget[used] = budget.get(used, 0) - 1
                                if budget[used] <= 0:
                                    passed_over.add(used)
                        if submitted >= count:
                            break
                    elif outcome.outcome == "already" and outcome.job_id:
                        stale = True  # the run moved on: the page is stale
                        break
                    elif outcome.outcome == "deferred" and outcome.node_wide:
                        passed_over.add(run.node_id)
                    if examined >= max_examined:
                        break
                if stale or len(page) < limit:
                    break
            if stale or submitted >= count or examined >= max_examined:
                break
        if self._wake is not None and submitted:
            self._wake()
        capped = examined >= max_examined and submitted < count and not stale
        return SubmitBatch(
            tuple(outcomes), cursor=last_seq if capped else 0, stale=stale
        )

    async def submit_run(
        self, campaign: CampaignRecord, run: RunRow, *, owner_name: str | None
    ) -> RunOutcome:
        try:
            # V0.10 G8: a split family's tree carries its generated data (up to
            # 64 MiB): built off the event loop
            built = await asyncio.to_thread(
                bench_tree, run.family, dict(run.params), run.seed
            )
        except (ParamsError, UnknownFamilyError, ManifestError, TreeError) as exc:
            return await self._skip(run, f"the workload library refuses it: {exc}")
        try:
            condition = parse_condition(run.conditions[0] if run.conditions else "none")
        except ConditionError as exc:  # pragma: no cover - stored from a parsed spec
            return await self._skip(run, f"its condition is invalid: {exc}")
        placement = run_placement(run)
        split: SplitRecord | None = None
        try:
            async with self._blobs.gc_guard.shared():
                await self._store_tree(built)
                # V0.10 G8: a mode3 run needs every node of its set
                for node_id in run.node_set or (run.node_id,):
                    node = await self._registry.get_node(node_id)
                    live = self._manager.get(node_id)
                    guarded = owner_guarded(live)
                    if guarded is not None:
                        return _guard_deferral(run, guarded)
                    try:
                        check_placement(
                            built.manifest,
                            node,
                            connected=live is not None,
                            policy=None if live is None else live.policy,
                            owner_name=owner_name,
                        )
                    except PlacementError as exc:
                        return await self._refused(run, exc)
                if built.manifest.split is not None:
                    # D278: a split family resolves its items like any split
                    # job - the same resolver, caps and stored item list
                    try:
                        split, _items = await resolve_split(
                            built.manifest,
                            project=built.tree,
                            inputs={},
                            blobs=self._blobs,
                            limits=self._split_limits,
                        )
                    except ManifestError as exc:
                        return await self._skip(
                            run, f"its split items do not resolve: {exc.message}"
                        )
                # V0.9 G4 (D217): the controller's own conditions come first -
                # fillers / a warm-up queued ahead of the run, the origin paused
                for key in _pre_jobs(run.id, condition, run.generation):
                    await self._create(
                        campaign, run, built, key=key, placement=placement
                    )  # (fillers / warm-ups: only whole-run families have conditions)
                if isinstance(condition, Unavailable):
                    if self._pauses is None:
                        return await self._skip(
                            run, "this controller cannot pause nodes"
                        )
                    self._pauses.pause(run.origin_node_id, condition.seconds)
                job, created = await self._create(
                    campaign,
                    run,
                    built,
                    key=run_key(run.id, run.generation),
                    placement=placement,
                    split=split,
                )
        except IdempotencyConflictError:
            # Only a request for another run (or a hand-made one) can hold this
            # run's key: never link a job that is not this run's.
            return await self._skip(run, "its idempotency key is held by another job")
        except (BlobError, TreeError) as exc:
            raise BenchSubmitError(
                f"storing the benchmark job failed: {exc}",
                details={"run_id": run.id, "cause": type(exc).__name__},
            ) from exc
        except MeshForgeError as exc:
            if exc.failure_type is FailureType.NETWORK_ERROR:
                raise BenchSubmitError(
                    "the database is unavailable", details={"run_id": run.id}
                ) from exc
            raise
        linked = await self._store.mark_submitted(
            run.id, job.id, generation=run.generation
        )
        swept = False
        if not linked:
            # CI fix round 4: the store's orphan sweep (``sync()``, every runner
            # tick) can link the run to the job this call just created between
            # the job's commit and this link. The run then IS submitted by this
            # call: report ``submitted``, not ``already`` (which stopped the
            # batch as stale). Only the creator may claim it, and only a
            # sweep's link: a concurrent submit that linked the same job itself
            # already reported ``submitted`` - one run is counted once.
            linked_to, by_sweep = await self._store.run_link(run.id)
            if created and linked_to == job.id and by_sweep:
                linked = swept = True
            elif created and linked_to != job.id:
                await self._unlinked(run, job.id, linked_to)
        log.info(
            "controller.bench.run_submitted",
            campaign_id=campaign.id,
            run_id=run.id,
            seq=run.seq,
            job_id=job.id,
            node_id=run.node_id,
            strategy=run.strategy,
            replay=not created,
            linked=linked,
            linked_by_sweep=swept,
            generation=run.generation,
        )
        if not linked:
            return RunOutcome(run.id, run.seq, "already", job_id=job.id)
        return RunOutcome(run.id, run.seq, "submitted", job_id=job.id)

    async def _unlinked(self, run: RunRow, job_id: str, linked_to: str | None) -> None:
        """Fix r1 (D250 D-1): this call created ``job_id`` but the run had moved
        on (cancelled -> skipped, or linked to another job: ``linked_to``) - the
        job is an orphan: cancel it now (``sync()`` is the backstop if this
        cancel fails). A run linked to ``job_id`` itself is not an orphan and
        never gets here."""
        self._store.expect_orphans()  # G5c item 0.7: the backstop looks now
        log.warning(
            "controller.bench.orphan_job_cancelled",
            run_id=run.id,
            job_id=job_id,
            linked_to=linked_to,
        )
        if self.cancel_job is not None:
            try:
                await self.cancel_job(job_id)
            except Exception:  # the sync backstop retries it
                log.exception("controller.bench.orphan_cancel_failed", job_id=job_id)

    async def _create(
        self,
        campaign: CampaignRecord,
        run: RunRow,
        built: BuiltTree,
        *,
        key: str,
        placement: dict[str, Any],
        split: SplitRecord | None = None,
    ) -> tuple[JobRecord, bool]:
        """One job of ``run``'s workload under ``key`` (the run job itself, a
        filler or a warm-up - the same path, the same lowest priority)."""
        canonical = built.manifest.canonical_dict()
        tree_id = str(built.tree.tree_id)
        return await self._jobs.create_queued_job(
            job_id=str(new_job_id()),
            name=built.manifest.name,
            manifest=canonical,
            bundle_digest=None,
            placement=placement,
            request_hash=_request_hash(run, canonical, tree_id, placement, key=key),
            idempotency_key=key,
            tree_id=tree_id,
            scheduling=JobScheduling(
                priority=MIN_PRIORITY,
                policy=built.manifest.policy,
                max_attempts=built.manifest.retry.max_attempts,
                retry_backoff_s=built.manifest.retry.backoff_s,
                on_lost_retry=built.manifest.retry.on_lost,
                submitter_node_id=run.origin_node_id,
                submitter_seeded=False,
                input_bytes=built.tree.total_bytes,
                fingerprint=workload_fingerprint(
                    built.manifest,
                    project=tree_id,
                    env_key=env_key(canonical, tree_id=tree_id, bundle_digest=None),
                ),
            ),
            owner_user_id=campaign.created_by,
            split=split,
        )

    async def _store_tree(self, built: BuiltTree) -> None:
        for hex_digest in [
            d.hex for d in self._blobs.missing(Digest(h) for h in built.blobs)
        ]:
            data = built.blobs[hex_digest]
            await self._blobs.put(
                Digest(hex_digest), _once(data), declared_size=len(data)
            )
        tree_digest = built.tree.tree_id
        if self._blobs.size(tree_digest) is None:
            await self._blobs.put(
                tree_digest,
                _once(built.manifest_bytes),
                declared_size=len(built.manifest_bytes),
            )
        stored = await self._blobs.tree(tree_digest)  # parsed like a submitted tree
        if stored is None or stored.tree_id != tree_digest:  # pragma: no cover
            raise TreeError("the benchmark tree did not round-trip the blob store")

    async def _refused(self, run: RunRow, exc: PlacementError) -> RunOutcome:
        refusal = str(exc.details.get("reject_code") or "")
        if exc.failure_type is FailureType.NOT_FOUND:
            return await self._skip(run, "the node no longer exists")
        if not refusal or refusal.startswith(_TRANSIENT_CODES_PREFIX):
            log.info(
                "controller.bench.run_deferred",
                run_id=run.id,
                node_id=run.node_id,
                reason=exc.message,
            )
            return RunOutcome(
                run.id,
                run.seq,
                "deferred",
                detail=exc.message,
                node_wide=refusal in NODE_WIDE_DEFERRALS,
            )
        return await self._skip(
            run, f"the node cannot run it: {exc.message} ({refusal})"
        )

    async def _skip(self, run: RunRow, detail: str) -> RunOutcome:
        moved = await self._store.mark_skipped(run.id, detail)
        log.warning(
            "controller.bench.run_skipped",
            campaign_id=run.campaign_id,
            run_id=run.id,
            node_id=run.node_id,
            detail=detail[:200],
        )
        return RunOutcome(
            run.id, run.seq, "skipped" if moved else "already", detail=detail
        )
