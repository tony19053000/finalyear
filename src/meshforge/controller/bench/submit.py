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

**Outcomes** (:class:`RunOutcome`): ``submitted`` (a job, linked);
``deferred`` (the node is offline / has no session / its owner's policy refuses
jobs now: the run stays ``planned`` and the next submit tries it again);
``skipped`` (the run can never run as planned: node deleted, cannot give the
job what it needs, the library refuses the params - the run becomes
``skipped`` with the reason); ``already`` (another submit moved it first).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Final, Literal

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
from meshforge.common.tree import Tree, TreeEntry, TreeError, canonical_bytes
from meshforge.controller.bench.conditions import NodePauses, filler_key, warmup_key
from meshforge.controller.bench.store import (
    BENCH_KEY_PREFIX,
    BenchStore,
    CampaignRecord,
    RunRow,
)
from meshforge.controller.blobs import BlobError, BlobStore
from meshforge.controller.jobs.env_health import env_key
from meshforge.controller.jobs.placement import PlacementError, check_placement
from meshforge.controller.jobs.store import (
    IdempotencyConflictError,
    JobRecord,
    JobScheduling,
    JobStore,
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


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _pre_jobs(run_id: str, condition: Condition) -> list[str]:
    """Keys of the jobs a controller-side condition queues before the run."""
    if isinstance(condition, QueueCondition):
        return [filler_key(run_id, i) for i in range(1, condition.jobs + 1)]
    if isinstance(condition, CacheCondition) and condition.mode == "warm":
        return [warmup_key(run_id)]
    return []


def _request_hash(
    run: RunRow,
    manifest: dict[str, object],
    tree_id: str,
    placement: dict[str, str],
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
    ) -> None:
        self._pauses = pauses
        """V0.9 G4: where ``unavailable:T`` pauses a run's origin."""
        self._store = store
        self._jobs = jobs
        self._blobs = blobs
        self._registry = registry
        self._manager = manager
        self._sessions = create_session_factory(engine)
        self._wake = wake

    async def _owner_name(self, user_id: str | None) -> str | None:
        if user_id is None:
            return None
        async with self._sessions() as db:
            name = await db.scalar(select(AppUser.name).where(AppUser.id == user_id))
        return None if name is None else str(name)

    async def submit_next(
        self, campaign: CampaignRecord, count: int
    ) -> list[RunOutcome]:
        """Submit up to ``count`` planned runs, in ``seq`` order. Raises
        :class:`BenchSubmitError` (nothing after the failing run is tried).

        **No head-of-line stall** (G4 item 0.5, D239.1): a *deferred* run does
        not use up ``count`` - the call keeps paging through the planned runs
        until ``count`` are submitted or the runs are exhausted. A node that
        deferred a run for a **node-wide** reason (offline, no live session,
        its owner accepts no jobs / not from this campaign's owner:
        :data:`NODE_WIDE_DEFERRALS`) is passed over for the rest of the call,
        in SQL - its later runs are neither tried nor listed, so a busy node
        holding the head of a 100 000-run campaign costs one try. A deferral
        that depends on the job (an owner RAM / CPU limit...) is per run. At
        most :data:`MAX_EXAMINED` runs are examined per call; skipped and
        ``already`` runs are listed but do not count as submitted."""
        owner_name = await self._owner_name(campaign.created_by)
        outcomes: list[RunOutcome] = []
        passed_over: set[str] = set()
        after_seq = 0
        examined = submitted = 0
        while submitted < count and examined < MAX_EXAMINED:
            limit = min(_PAGE, MAX_EXAMINED - examined)
            page = await self._store.runs(
                campaign.id,
                state="planned",
                after_seq=after_seq,
                limit=limit,
                exclude_nodes=sorted(passed_over),
            )
            if not page:
                break
            for run in page:
                after_seq = run.seq
                if run.node_id in passed_over:
                    continue  # deferred earlier on this page
                examined += 1
                outcome = await self.submit_run(campaign, run, owner_name=owner_name)
                outcomes.append(outcome)
                if outcome.outcome == "submitted":
                    submitted += 1
                    if submitted >= count:
                        break
                elif outcome.outcome == "deferred" and outcome.node_wide:
                    passed_over.add(run.node_id)
            if len(page) < limit:
                break
        if self._wake is not None and submitted:
            self._wake()
        return outcomes

    async def submit_run(
        self, campaign: CampaignRecord, run: RunRow, *, owner_name: str | None
    ) -> RunOutcome:
        try:
            built = bench_tree(run.family, dict(run.params), run.seed)
        except (ParamsError, UnknownFamilyError, ManifestError, TreeError) as exc:
            return await self._skip(run, f"the workload library refuses it: {exc}")
        try:
            condition = parse_condition(run.conditions[0] if run.conditions else "none")
        except ConditionError as exc:  # pragma: no cover - stored from a parsed spec
            return await self._skip(run, f"its condition is invalid: {exc}")
        placement = {
            "mode": "local" if run.node_id == run.origin_node_id else "offload",
            "node_id": run.node_id,
        }
        try:
            async with self._blobs.gc_guard.shared():
                await self._store_tree(built)
                node = await self._registry.get_node(run.node_id)
                live = self._manager.get(run.node_id)
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
                # V0.9 G4 (D217): the controller's own conditions come first -
                # fillers / a warm-up queued ahead of the run, the origin paused
                for key in _pre_jobs(run.id, condition):
                    await self._create(
                        campaign, run, built, key=key, placement=placement
                    )
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
                    key=IDEMPOTENCY_PREFIX + run.id,
                    placement=placement,
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
        linked = await self._store.mark_submitted(run.id, job.id)
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
        )
        if not linked:
            return RunOutcome(run.id, run.seq, "already", job_id=job.id)
        return RunOutcome(run.id, run.seq, "submitted", job_id=job.id)

    async def _create(
        self,
        campaign: CampaignRecord,
        run: RunRow,
        built: BuiltTree,
        *,
        key: str,
        placement: dict[str, str],
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
