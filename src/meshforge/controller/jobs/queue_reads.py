"""What one dispatch tick reads, inside the dispatch transaction (V0.6 G2,
D104-D106, D108, D112).

:func:`queued_jobs` picks the batch in **queue order** (D106): effective
priority first, then the time the job entered the queue. Effective priority =
``LEAST(10, priority + floor(waited / priority_aging_s))`` - waiting raises a
job by one level per aging period up to the top level, so even a ``-10`` job
reaches the front eventually (ties go to the longer wait). Jobs whose retry
``not_before`` is still ahead are skipped. ``FOR UPDATE SKIP LOCKED`` as since
V0.4: two dispatchers never take one job.

:func:`read_fabric` reads, for that batch, everything the planner needs from
PostgreSQL into a :class:`FabricRows`:

* every node row (state, hardware, scores, advertised slots) - at most
  :data:`MAX_NODES`;
* **slot use derived from live attempts** (D105): per node, live attempts
  holding a GPU slot and those holding a CPU slot (an attempt from before
  V0.6 has no slot and counts as CPU), and the GPU indices their start
  snapshots name (one job per GPU, so two GPU slots get two GPUs). No
  in-memory counter that could drift: a controller restart sees exactly what
  is running;
* which nodes hold which of the batch's trees whole (``cache_location``);
* the batch's named-input sizes;
* the run-time history of the batch's workload fingerprints (D108), and (V0.6
  G4) their median uploaded bytes and the env hash their latest attempt on
  each node reported, with which of those environments each node still holds;
* the environment build-time history of the batch's ``env.base`` values (env
  phases of at least :data:`ENV_BUILD_MIN_MS` - shorter ones were cache hits);
* the network matrix (``network_link``) and the controller -> node bandwidth
  from transfer history (V0.6 G4, D111);
* the reason key of each job's latest decision (D104: a waiting job's
  decision is recorded again only when it changes);
* the failed / lost attempts of the batch's retried jobs (V0.6 G3, D110:
  where a retry should not go).

Every read is bounded. :meth:`FabricRows.took` updates the slot counts when the
tick creates an attempt, so a later job in the same tick sees the slot taken.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from sqlalchemy import DateTime, Float, and_, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.manifest import MAX_PRIORITY
from meshforge.common.states import AttemptPhase, AttemptState, JobState
from meshforge.controller.network.store import controller_links, read_links
from meshforge.controller.planner.retry_exclusion import PastFailure
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    ControllerLinkStat,
    EnvBuildStat,
    History,
    LinkStat,
)
from meshforge.controller.store.models.cache import CacheLocation
from meshforge.controller.store.models.data import JobInput, NamedInput
from meshforge.controller.store.models.job import Attempt, AttemptPhaseRow, Job
from meshforge.controller.store.models.node import Node
from meshforge.controller.store.models.plan import PlanDecision

__all__ = [
    "ENV_BUILD_MIN_MS",
    "MAX_HISTORY_FINGERPRINTS",
    "MAX_NODES",
    "FabricRows",
    "NetworkReads",
    "NodeRow",
    "queued_jobs",
    "read_fabric",
]

MAX_NODES: Final = 1000
MAX_HISTORY_FINGERPRINTS: Final = 500
_MAX_HOLDINGS: Final = 100_000
_MAX_ENV_HOLDINGS: Final = 100_000
_DEFAULT_BASE: Final = "python-cpu"
"""The manifest's ``env.base`` default (a stored manifest may omit it)."""
ENV_BUILD_MIN_MS: Final = 1000
"""An env phase shorter than this was a cache hit, not a build."""
_MAX_PAST_PER_JOB: Final = 16
"""More than the manifest's 10 attempts, so no job's failures are cut."""


@dataclass(frozen=True, slots=True)
class NetworkReads:
    """``[controller.network]`` values the tick's reads need (D111)."""

    transfer_min_bytes: int = 1024 * 1024
    transfer_history: int = 20


@dataclass(frozen=True, slots=True)
class NodeRow:
    node_id: str
    state: str
    hardware: Mapping[str, Any]
    hardware_scores: Mapping[str, Any] | None
    cpu_slots: int | None
    gpu_slots: int | None


@dataclass(slots=True)
class FabricRows:
    """One tick's database view (module docstring)."""

    nodes: dict[str, NodeRow]
    used: dict[str, list[int]] = field(default_factory=dict)
    """node_id -> [cpu slots used, gpu slots used] by live attempts."""
    gpus_held: dict[str, set[int]] = field(default_factory=dict)
    """node_id -> NVML indices in the start snapshots of its live attempts."""
    holdings: dict[str, frozenset[str]] = field(default_factory=dict)
    """node_id -> the batch's tree ids / digests it holds whole."""
    input_trees: dict[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    """job_id -> ((tree_id, size), ...) of its named inputs."""
    history: dict[str, History] = field(default_factory=dict)
    """fingerprint -> past run times."""
    last_reason: dict[str, str] = field(default_factory=dict)
    """job_id -> reason_key of its latest plan_decision."""
    past_failures: dict[str, tuple[PastFailure, ...]] = field(default_factory=dict)
    """job_id -> its ended ``failed``/``lost`` attempts (retried jobs only)."""
    env_holdings: dict[str, dict[str, int]] = field(default_factory=dict)
    """node_id -> {env hash: bytes} of the batch's environments it holds."""
    env_builds: dict[str, EnvBuildStat] = field(default_factory=dict)
    """``env.base`` -> build-time history (V0.6 G4)."""
    links: dict[tuple[str, str], LinkStat] = field(default_factory=dict)
    """(src, dst) -> the network matrix row (V0.6 G4, D111)."""
    controller_links: dict[str, ControllerLinkStat] = field(default_factory=dict)
    """node_id -> controller -> node bandwidth from transfer history."""
    snapshot: ClusterSnapshot | None = None
    """The planner snapshot built from these rows (the dispatcher's, cached
    for the tick and patched by :meth:`took`)."""

    def used_slots(self, node_id: str) -> tuple[int, int]:
        cpu, gpu = self.used.get(node_id, (0, 0))
        return cpu, gpu

    def busy_gpus(self, node_id: str) -> frozenset[int]:
        return frozenset(self.gpus_held.get(node_id, ()))

    def took(self, node_id: str, slot: str, gpu_indices: Sequence[int] = ()) -> None:
        """An attempt of this tick now holds ``slot`` (and ``gpu_indices``)
        on ``node_id``."""
        counts = self.used.setdefault(node_id, [0, 0])
        counts[1 if slot == "gpu" else 0] += 1
        held = self.gpus_held.setdefault(node_id, set())
        held.update(gpu_indices)
        if self.snapshot is None:
            return
        nodes = tuple(
            replace(
                n,
                used_cpu_slots=n.used_cpu_slots + (slot != "gpu"),
                used_gpu_slots=n.used_gpu_slots + (slot == "gpu"),
                live_attempts=n.live_attempts + 1,
                busy_gpus=n.busy_gpus | held,
            )
            if n.node_id == node_id
            else n
            for n in self.snapshot.nodes
        )
        self.snapshot = replace(self.snapshot, nodes=nodes)


def _now(now: dt.datetime) -> Any:
    return literal(now, DateTime(timezone=True))


def _effective_priority(now: dt.datetime, aging_s: float) -> Any:
    waited = func.date_part(
        "epoch", _now(now) - func.coalesce(Job.queued_at, Job.created_at)
    )
    aged = func.floor(cast(waited, Float) / aging_s)
    return func.least(MAX_PRIORITY, Job.priority + func.greatest(aged, 0))


async def queued_jobs(
    db: AsyncSession, *, limit: int, now: dt.datetime, aging_s: float
) -> list[Job]:
    """The batch, locked, in queue order (module docstring)."""
    effective = _effective_priority(now, aging_s)
    return list(
        await db.scalars(
            select(Job)
            .where(
                Job.state == JobState.QUEUED.value,
                or_(Job.not_before.is_(None), Job.not_before <= _now(now)),
            )
            .order_by(
                effective.desc(),
                func.coalesce(Job.queued_at, Job.created_at),
                Job.id,
            )
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    )


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values))


async def _history(
    db: AsyncSession, fingerprints: Sequence[str], runs: int
) -> dict[str, History]:
    if not fingerprints:
        return {}
    ranked = (
        select(
            Job.fingerprint.label("fp"),
            Attempt.node_id.label("node_id"),
            AttemptPhaseRow.duration_ms.label("ms"),
            func.row_number()
            .over(
                partition_by=(Job.fingerprint, Attempt.node_id),
                order_by=(Attempt.ended_at.desc(), Attempt.id),
            )
            .label("n"),
        )
        .join(Attempt, Attempt.job_id == Job.id)
        .join(AttemptPhaseRow, AttemptPhaseRow.attempt_id == Attempt.id)
        .where(
            Job.fingerprint.in_(fingerprints[:MAX_HISTORY_FINGERPRINTS]),
            Attempt.state == AttemptState.DONE.value,
            AttemptPhaseRow.phase == AttemptPhase.RUN.value,
            AttemptPhaseRow.skipped.is_(False),
        )
        .subquery()
    )
    rows = await db.execute(
        select(ranked.c.fp, ranked.c.node_id, ranked.c.ms).where(ranked.c.n <= runs)
    )
    grouped: dict[str, dict[str, list[float]]] = {}
    for fp, node_id, ms in rows.all():
        grouped.setdefault(str(fp), {}).setdefault(str(node_id), []).append(
            int(ms) / 1000.0
        )
    outputs = await _output_history(db, fingerprints, runs)
    envs = await _env_hashes(db, fingerprints)
    out: dict[str, History] = {}
    for fp in sorted(set(grouped) | set(outputs) | set(envs)):
        per_node = grouped.get(fp, {})
        sizes = outputs.get(fp, [])
        out[fp] = History(
            by_node_s={n: _median(v) for n, v in sorted(per_node.items())},
            samples_by_node={n: len(v) for n, v in sorted(per_node.items())},
            output_bytes=_median(sizes) if sizes else None,
            output_samples=len(sizes),
            env_hash_by_node=dict(sorted(envs.get(fp, {}).items())),
        )
    return out


async def _output_history(
    db: AsyncSession, fingerprints: Sequence[str], runs: int
) -> dict[str, list[float]]:
    """fingerprint -> uploaded bytes of its latest ``runs`` DONE attempts."""
    ranked = (
        select(
            Job.fingerprint.label("fp"),
            Attempt.output_bytes.label("size"),
            func.row_number()
            .over(
                partition_by=Job.fingerprint,
                order_by=(Attempt.ended_at.desc(), Attempt.id),
            )
            .label("n"),
        )
        .join(Attempt, Attempt.job_id == Job.id)
        .where(
            Job.fingerprint.in_(fingerprints[:MAX_HISTORY_FINGERPRINTS]),
            Attempt.state == AttemptState.DONE.value,
            Attempt.output_bytes.is_not(None),
        )
        .subquery()
    )
    rows = await db.execute(
        select(ranked.c.fp, ranked.c.size).where(ranked.c.n <= runs)
    )
    out: dict[str, list[float]] = {}
    for fp, size in rows.all():
        out.setdefault(str(fp), []).append(float(size))
    return out


async def _env_hashes(
    db: AsyncSession, fingerprints: Sequence[str]
) -> dict[str, dict[str, str]]:
    """fingerprint -> {node_id: env hash its latest attempt there reported}."""
    rows = await db.execute(
        select(Job.fingerprint, Attempt.node_id, Attempt.env_hash)
        .join(Attempt, Attempt.job_id == Job.id)
        .where(
            Job.fingerprint.in_(fingerprints[:MAX_HISTORY_FINGERPRINTS]),
            Attempt.env_hash.is_not(None),
        )
        .distinct(Job.fingerprint, Attempt.node_id)
        .order_by(Job.fingerprint, Attempt.node_id, Attempt.ended_at.desc())
        .limit(MAX_HISTORY_FINGERPRINTS * MAX_NODES)
    )
    out: dict[str, dict[str, str]] = {}
    for fp, node_id, env_hash in rows.all():
        out.setdefault(str(fp), {})[str(node_id)] = str(env_hash)
    return out


async def _env_builds(
    db: AsyncSession, bases: Sequence[str], runs: int
) -> dict[str, EnvBuildStat]:
    """``env.base`` -> median env phase of its latest ``runs`` builds."""
    if not bases:
        return {}
    base = func.coalesce(Job.manifest["env"]["base"].astext, _DEFAULT_BASE)
    ranked = (
        select(
            base.label("base"),
            AttemptPhaseRow.duration_ms.label("ms"),
            func.row_number()
            .over(partition_by=base, order_by=AttemptPhaseRow.started_at.desc())
            .label("n"),
        )
        .join(Attempt, Attempt.job_id == Job.id)
        .join(AttemptPhaseRow, AttemptPhaseRow.attempt_id == Attempt.id)
        .where(
            base.in_(sorted(bases)),
            AttemptPhaseRow.phase == AttemptPhase.ENV.value,
            AttemptPhaseRow.skipped.is_(False),
            AttemptPhaseRow.duration_ms >= ENV_BUILD_MIN_MS,
        )
        .subquery()
    )
    rows = await db.execute(
        select(ranked.c.base, ranked.c.ms).where(ranked.c.n <= runs)
    )
    grouped: dict[str, list[float]] = {}
    for name, ms in rows.all():
        grouped.setdefault(str(name), []).append(int(ms) / 1000.0)
    return {
        name: EnvBuildStat(median_s=_median(v), samples=len(v))
        for name, v in sorted(grouped.items())
    }


async def _network(db: AsyncSession, reads: NetworkReads, fabric: FabricRows) -> None:
    fabric.links = {
        (r.src_node_id, r.dst_node_id): LinkStat(
            bw_bytes_s=r.bw_bytes_s,
            rtt_ms=r.rtt_ms,
            samples=r.samples,
            measured_at=r.measured_at,
        )
        for r in await read_links(db)
    }
    fabric.controller_links = {
        node: ControllerLinkStat(bw_bytes_s=c.bw_bytes_s, samples=c.samples)
        for node, c in (
            await controller_links(
                db,
                min_bytes=reads.transfer_min_bytes,
                per_node=reads.transfer_history,
            )
        ).items()
    }


async def _gpus_held(
    db: AsyncSession, live_states: Sequence[str]
) -> dict[str, set[int]]:
    """Per node, the GPU indices its live attempts were given (any slot kind:
    an attempt from before V0.6 has no slot but may hold a GPU)."""
    indices = Attempt.start_snapshot["gpu_indices"]
    rows = await db.execute(
        select(Attempt.node_id, indices).where(
            Attempt.state.in_(live_states),
            func.jsonb_typeof(indices) == "array",
        )
    )
    held: dict[str, set[int]] = {}
    for node_id, value in rows.all():
        for index in value if isinstance(value, list) else ():
            if isinstance(index, int) and not isinstance(index, bool):
                held.setdefault(str(node_id), set()).add(index)
    return held


async def read_fabric(
    db: AsyncSession,
    jobs: Sequence[Job],
    *,
    history_runs: int,
    keys_of: Callable[[Job], Sequence[str]],
    network: NetworkReads | None = None,
) -> FabricRows:
    """See module docstring. ``keys_of(job)`` = the job's project tree (or
    bundle) key; its named inputs are added here."""
    node_rows = await db.scalars(select(Node).order_by(Node.node_id).limit(MAX_NODES))
    fabric = FabricRows(
        nodes={
            n.node_id: NodeRow(
                node_id=n.node_id,
                state=n.state,
                hardware=dict(n.hardware),
                hardware_scores=n.hardware_scores,
                cpu_slots=n.cpu_slots,
                gpu_slots=n.gpu_slots,
            )
            for n in node_rows
        }
    )
    live_states = [s.value for s in AttemptState if s.value in _LIVE]
    usage = await db.execute(
        select(
            Attempt.node_id,
            func.count().filter(Attempt.slot == "gpu"),
            func.count().filter(or_(Attempt.slot.is_(None), Attempt.slot == "cpu")),
        )
        .where(Attempt.state.in_(live_states))
        .group_by(Attempt.node_id)
    )
    for node_id, gpu, cpu in usage.all():
        fabric.used[str(node_id)] = [int(cpu), int(gpu)]
    fabric.gpus_held = await _gpus_held(db, live_states)
    job_ids = [j.id for j in jobs]
    if not job_ids:
        return fabric
    inputs = await db.execute(
        select(JobInput.job_id, JobInput.tree_id, NamedInput.size)
        .join(
            NamedInput,
            and_(
                NamedInput.name == JobInput.name,
                NamedInput.version == JobInput.version,
            ),
        )
        .where(JobInput.job_id.in_(job_ids))
        .order_by(JobInput.job_id, JobInput.mount)
    )
    per_job: dict[str, list[tuple[str, int]]] = {}
    for job_id, tree_id, size in inputs.all():
        per_job.setdefault(str(job_id), []).append((str(tree_id), int(size)))
    fabric.input_trees = {k: tuple(v) for k, v in per_job.items()}
    keys = {k for job in jobs for k in keys_of(job)}
    keys |= {tree for trees in per_job.values() for tree, _ in trees}
    if keys:
        held = await db.execute(
            select(CacheLocation.node_id, CacheLocation.key)
            .where(
                CacheLocation.kind.in_(("tree", "blob")),
                CacheLocation.key.in_(sorted(keys)),
            )
            .limit(_MAX_HOLDINGS)
        )
        holdings: dict[str, set[str]] = {}
        for node_id, key in held.all():
            holdings.setdefault(str(node_id), set()).add(str(key))
        fabric.holdings = {n: frozenset(k) for n, k in holdings.items()}
    fingerprints = sorted({j.fingerprint for j in jobs if j.fingerprint})
    fabric.history = await _history(db, fingerprints, history_runs)
    env_hashes = sorted(
        {h for hist in fabric.history.values() for h in hist.env_hash_by_node.values()}
    )
    if env_hashes:
        envs = await db.execute(
            select(CacheLocation.node_id, CacheLocation.key, CacheLocation.size)
            .where(CacheLocation.kind == "env", CacheLocation.key.in_(env_hashes))
            .limit(_MAX_ENV_HOLDINGS)
        )
        for node_id, key, size in envs.all():
            fabric.env_holdings.setdefault(str(node_id), {})[str(key)] = int(size)
    bases = sorted({_base_of(j) for j in jobs})
    fabric.env_builds = await _env_builds(db, bases, history_runs)
    await _network(db, network or NetworkReads(), fabric)
    last = await db.execute(
        select(PlanDecision.job_id, PlanDecision.reason_key)
        .where(PlanDecision.job_id.in_(job_ids))
        .distinct(PlanDecision.job_id)
        .order_by(PlanDecision.job_id, PlanDecision.id.desc())
    )
    fabric.last_reason = {str(j): str(r) for j, r in last.all()}
    fabric.past_failures = await _past_failures(
        db, [j.id for j in jobs if int(j.attempt_epoch) > 0]
    )
    return fabric


def _base_of(job: Job) -> str:
    env = job.manifest.get("env") if isinstance(job.manifest, dict) else None
    base = env.get("base") if isinstance(env, dict) else None
    return str(base or _DEFAULT_BASE)


async def _past_failures(
    db: AsyncSession, job_ids: Sequence[str]
) -> dict[str, tuple[PastFailure, ...]]:
    """D110: the ended attempts a retry should avoid (bounded: a job has at
    most ``MAX_ATTEMPTS`` attempts)."""
    if not job_ids:
        return {}
    indices = Attempt.start_snapshot["gpu_indices"]
    rows = await db.execute(
        select(
            Attempt.job_id,
            Attempt.node_id,
            Attempt.failure_type,
            Attempt.ended_at,
            indices,
        )
        .where(
            Attempt.job_id.in_(job_ids),
            Attempt.state.in_((AttemptState.FAILED.value, AttemptState.LOST.value)),
            Attempt.failure_type.is_not(None),
        )
        .order_by(Attempt.job_id, Attempt.fencing_token)
        .limit(len(job_ids) * _MAX_PAST_PER_JOB)
    )
    out: dict[str, list[PastFailure]] = {}
    for job_id, node_id, failure_type, ended_at, gpus in rows.all():
        held = tuple(
            i
            for i in (gpus if isinstance(gpus, list) else ())
            if isinstance(i, int) and not isinstance(i, bool)
        )
        out.setdefault(str(job_id), []).append(
            PastFailure(str(node_id), str(failure_type), ended_at, held)
        )
    return {k: tuple(v) for k, v in out.items()}


_LIVE: Final = frozenset(
    {
        AttemptState.ASSIGNED.value,
        AttemptState.STAGING.value,
        AttemptState.ENV_READY.value,
        AttemptState.RUNNING.value,
        AttemptState.UPLOADING.value,
    }
)
