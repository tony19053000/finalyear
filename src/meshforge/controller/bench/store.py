"""The one writer of ``bench_campaign`` / ``bench_run`` (V0.9 G3, D216/D232; migration 0022).

* :meth:`BenchStore.create_campaign` writes the campaign row and **every**
  planned run (the expansion, in ``seq`` order) in one transaction - a crash
  leaves no half campaign.
* Run state moves only forward, by compare-and-set on the old state:
  ``planned -> submitted`` (:meth:`~BenchStore.mark_submitted`, with the job's
  id), ``planned -> skipped`` (:meth:`~BenchStore.mark_skipped`), and
  ``submitted -> done | failed`` when the linked job is terminal
  (:meth:`~BenchStore.sync`, one ``UPDATE ... FROM job``; G3 runs it lazily on
  every campaign read and submit - there is no background loop until G5).
  ``sync`` also links a ``planned`` run whose job was committed but never
  linked (a crash in between, G4 item 0.6) and stamps ``ended_at`` with the
  job's final transition time (G4 item 0.7).
  A campaign is ``done`` once no run is ``planned`` or ``submitted``.
* :func:`campaign_identities` is the run-record recorder's lookup (D212):
  ``job_id -> {campaign_id, group_id, config_id, repetition, run_id}`` for a
  batch of jobs, one indexed query inside the recorder's own transaction. A
  job of an earlier **generation** of a requeued run (G5: the link moved to the
  new job) is found by its key ``bench:<run_id>[:<g>]``, checked like
  :meth:`BenchStore._link_orphans` checks one - so its record keeps the
  campaign (checklist 29).
* **Runner-owned campaigns** (V0.9 G5: ``started_at`` set): ``sync`` settles a
  finished run by :func:`.outcome.settle` instead - ``done``, ``failed``, or
  back to ``planned`` (infra retry / owner pre-emption, ``generation + 1``).

Callers get frozen records; database errors become :class:`BenchStoreError`
(``network_error``, retryable).
"""

from __future__ import annotations

import datetime as dt
import functools
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import String, case, cast, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.bench.campaign import Expansion, run_dict
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.ids import new_bench_run_id
from meshforge.common.logging import get_logger
from meshforge.common.states import JOB_TERMINAL_STATES, JobState
from meshforge.controller.bench.keys import run_key, run_of_key
from meshforge.controller.bench.outcome import settle
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.bench import (
    MAX_LAST_FAILURE_CHARS,
    MAX_RUN_DETAIL_CHARS,
    BenchCampaign,
    BenchRun,
)
from meshforge.controller.store.models.job import Job, JobTransition

__all__ = [
    "MAX_LIST",
    "BenchStore",
    "BenchStoreError",
    "CampaignRecord",
    "RunRow",
    "campaign_identities",
]

log = get_logger(__name__)

MAX_LIST: Final = 1_000
BENCH_KEY_PREFIX: Final = "bench:"
"""The Idempotency-Key prefix of a forced run's job (``bench:<run_id>``);
:data:`.submit.IDEMPOTENCY_PREFIX` is this value."""
_INSERT_CHUNK: Final = 1_000
_SETTLE_BATCH: Final = 500
_TERMINAL: Final = tuple(sorted(s.value for s in JOB_TERMINAL_STATES))
_OPEN_RUN_STATES: Final = ("planned", "submitted")
RUN_STATE_NAMES: Final = ("planned", "submitted", "done", "failed", "skipped")


class BenchStoreError(MeshForgeError):
    """The database refused or lost a campaign read/write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR


def _typed[**P, R](
    method: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @functools.wraps(method)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await method(*args, **kwargs)
        except MeshForgeError:
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise BenchStoreError(
                f"bench.{method.__name__} failed: {type(exc).__name__}",
                details={"operation": method.__name__, "cause": type(exc).__name__},
            ) from exc

    return wrapper


@dataclass(frozen=True, slots=True)
class CampaignRecord:
    id: str
    name: str
    spec_yaml: str
    spec_digest: str
    seed: int
    state: str
    run_count: int
    summary: Mapping[str, Any]
    created_by: str | None
    created_at: dt.datetime
    counts: Mapping[str, int] = field(default_factory=dict)
    """Runs per state (every state listed, zeros included)."""
    max_concurrent_per_node: int = 1
    cooldown_s: float = 5.0
    max_infra_retries: int = 2
    started_at: dt.datetime | None = None
    """Set = the runner owns the campaign (V0.9 G5)."""
    finished_reason: str | None = None

    @property
    def runner_owned(self) -> bool:
        return self.started_at is not None


@dataclass(frozen=True, slots=True)
class RunRow:
    id: str
    campaign_id: str
    seq: int
    group_id: str
    config_id: str
    repetition: int
    family: str
    params: Mapping[str, Any]
    seed: int
    conditions: tuple[str, ...]
    strategy: str
    node_id: str
    origin_node_id: str
    state: str
    job_id: str | None = None
    detail: str | None = None
    submitted_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    tries: int = 0
    generation: int = 0
    last_failure: str | None = None

    @property
    def job_key(self) -> str:
        """This generation's job Idempotency-Key."""
        return run_key(self.id, self.generation)


def _campaign(row: BenchCampaign, counts: Mapping[str, int]) -> CampaignRecord:
    return CampaignRecord(
        id=row.id,
        name=row.name,
        spec_yaml=row.spec_yaml,
        spec_digest=row.spec_digest,
        seed=int(row.seed),
        state=row.state,
        run_count=int(row.run_count),
        summary=dict(row.summary),
        created_by=row.created_by,
        created_at=row.created_at,
        counts={state: int(counts.get(state, 0)) for state in RUN_STATE_NAMES},
        max_concurrent_per_node=int(row.max_concurrent_per_node or 1),
        cooldown_s=float(row.cooldown_s if row.cooldown_s is not None else 5.0),
        max_infra_retries=int(
            row.max_infra_retries if row.max_infra_retries is not None else 2
        ),
        started_at=row.started_at,
        finished_reason=row.finished_reason,
    )


def _run(row: BenchRun) -> RunRow:
    return RunRow(
        id=row.id,
        campaign_id=row.campaign_id,
        seq=int(row.seq),
        group_id=row.group_id,
        config_id=row.config_id,
        repetition=int(row.repetition),
        family=row.family,
        params=dict(row.params),
        seed=int(row.seed),
        conditions=tuple(row.conditions),
        strategy=row.strategy,
        node_id=row.node_id,
        origin_node_id=row.origin_node_id,
        state=row.state,
        job_id=row.job_id,
        detail=row.detail,
        submitted_at=row.submitted_at,
        ended_at=row.ended_at,
        tries=int(row.tries or 0),
        generation=int(row.generation or 0),
        last_failure=row.last_failure,
    )


async def campaign_identities(
    db: AsyncSession, job_ids: Collection[str]
) -> dict[str, dict[str, Any]]:
    """``job_id -> {campaign_id, group_id, config_id, repetition, run_id}`` for
    the benchmark jobs among ``job_ids`` (the others are absent)."""
    if not job_ids:
        return {}
    rows = await db.execute(
        select(
            BenchRun.job_id,
            BenchRun.campaign_id,
            BenchRun.group_id,
            BenchRun.config_id,
            BenchRun.repetition,
            BenchRun.id,
        ).where(BenchRun.job_id.in_(sorted(job_ids)))
    )
    out = {
        str(job_id): {
            "campaign_id": campaign_id,
            "group_id": group_id,
            "config_id": config_id,
            "repetition": int(repetition),
            "run_id": run_id,
        }
        for job_id, campaign_id, group_id, config_id, repetition, run_id in rows
    }
    rest = [j for j in job_ids if j not in out]
    if rest:
        out.update(await _earlier_generations(db, rest))
    return out


async def _earlier_generations(
    db: AsyncSession, job_ids: Collection[str]
) -> dict[str, dict[str, Any]]:
    """V0.9 G5: jobs of a requeued run's earlier generations (the run's link
    moved on). Found by key and accepted only when the job **is** that run's
    (same checks as ``_link_orphans``: owner = the campaign's creator,
    submitted from the run's origin, pinned to its node)."""
    jobs = list(
        (
            await db.execute(
                select(
                    Job.id,
                    Job.idempotency_key,
                    Job.owner_user_id,
                    Job.submitter_node_id,
                    Job.placement,
                ).where(
                    Job.id.in_(sorted(job_ids)),
                    Job.idempotency_key.like(f"{BENCH_KEY_PREFIX}%"),
                )
            )
        ).tuples()
    )
    by_run: dict[str, list[tuple[str, str | None, str | None, Any]]] = {}
    for job_id, key, owner, submitter, placement in jobs:
        found = run_of_key(key)
        if found is not None:
            by_run.setdefault(found[0], []).append(
                (job_id, owner, submitter, placement)
            )
    if not by_run:
        return {}
    rows = await db.execute(
        select(
            BenchRun.id,
            BenchRun.campaign_id,
            BenchRun.group_id,
            BenchRun.config_id,
            BenchRun.repetition,
            BenchRun.node_id,
            BenchRun.origin_node_id,
            BenchCampaign.created_by,
        )
        .join(BenchCampaign, BenchCampaign.id == BenchRun.campaign_id)
        .where(BenchRun.id.in_(sorted(by_run)))
    )
    out: dict[str, dict[str, Any]] = {}
    for run_id, cid, group, config, rep, node, origin, creator in rows:
        for job_id, owner, submitter, placement in by_run.get(run_id, ()):
            pinned = (
                (placement or {}).get("node_id")
                if isinstance(placement, dict)
                else None
            )
            if owner != creator or submitter != origin or pinned != node:
                continue
            out[str(job_id)] = {
                "campaign_id": cid,
                "group_id": group,
                "config_id": config,
                "repetition": int(rep),
                "run_id": run_id,
            }
    return out


class BenchStore:
    """Async repository for campaigns and their runs (see module docstring)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    # ------------------------------------------------------------ campaigns

    @_typed
    async def create_campaign(
        self,
        *,
        campaign_id: str,
        name: str,
        spec_yaml: str,
        spec_digest: str,
        seed: int,
        expansion: Expansion,
        nodes: Sequence[str],
        created_by: str | None,
        session_label: str | None = None,
    ) -> CampaignRecord:
        summary = {
            "groups": expansion.groups,
            "configs": expansion.configs,
            "dropped": expansion.dropped,
            "duplicates": expansion.duplicates,
            "estimated_hours": expansion.estimated_hours,
            "nodes": sorted(nodes),
            # V0.9 G4 (D241): copied to every record of the campaign
            "session_label": session_label,
            "unsupported": expansion.unsupported,
        }
        async with self._sessions() as db, db.begin():
            db.add(
                BenchCampaign(
                    id=campaign_id,
                    name=name,
                    spec_yaml=spec_yaml,
                    spec_digest=spec_digest,
                    seed=seed,
                    state="active",
                    run_count=len(expansion.runs),
                    summary=summary,
                    created_by=created_by,
                )
            )
            await db.flush()
            rows: list[dict[str, Any]] = []
            for run in expansion.runs:
                data = run_dict(run)
                skipped = run.skip_reason is not None
                rows.append(
                    {
                        "id": new_bench_run_id(),
                        "campaign_id": campaign_id,
                        "seq": data["seq"],
                        "group_id": data["group_id"],
                        "config_id": data["config_id"],
                        "repetition": data["repetition"],
                        "family": data["family"],
                        "params": data["params"],
                        "seed": data["seed"],
                        "conditions": data["conditions"],
                        "strategy": data["strategy"],
                        "node_id": data["node_id"],
                        "origin_node_id": data["origin_node_id"],
                        # V0.9 G4: on_unsupported: skip -> stored skipped
                        "state": "skipped" if skipped else "planned",
                        "detail": None
                        if run.skip_reason is None
                        else f"unsupported: {run.skip_reason}"[:MAX_RUN_DETAIL_CHARS],
                        "ended_at": dt.datetime.now(dt.UTC) if skipped else None,
                    }
                )
            for start in range(0, len(rows), _INSERT_CHUNK):
                await db.execute(insert(BenchRun), rows[start : start + _INSERT_CHUNK])
            row = await db.get_one(BenchCampaign, campaign_id)
            skipped_runs = sum(1 for r in rows if r["state"] == "skipped")
            return _campaign(
                row, {"planned": len(rows) - skipped_runs, "skipped": skipped_runs}
            )

    async def _counts(
        self, db: AsyncSession, campaign_ids: Sequence[str]
    ) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {cid: {} for cid in campaign_ids}
        if not campaign_ids:
            return out
        for cid, state, count in await db.execute(
            select(BenchRun.campaign_id, BenchRun.state, func.count())
            .where(BenchRun.campaign_id.in_(campaign_ids))
            .group_by(BenchRun.campaign_id, BenchRun.state)
        ):
            out[cid][state] = int(count)
        return out

    @_typed
    async def get_campaign(self, campaign_id: str) -> CampaignRecord | None:
        async with self._sessions() as db:
            row = await db.get(BenchCampaign, campaign_id)
            if row is None:
                return None
            counts = await self._counts(db, [campaign_id])
            return _campaign(row, counts[campaign_id])

    @_typed
    async def list_campaigns(self, *, limit: int = 100) -> list[CampaignRecord]:
        """Newest first."""
        limit = max(1, min(limit, MAX_LIST))
        async with self._sessions() as db:
            rows = list(
                await db.scalars(
                    select(BenchCampaign)
                    .order_by(BenchCampaign.created_at.desc(), BenchCampaign.id.desc())
                    .limit(limit)
                )
            )
            counts = await self._counts(db, [r.id for r in rows])
            return [_campaign(r, counts[r.id]) for r in rows]

    # ------------------------------------------------------------ runs

    @_typed
    async def runs(
        self,
        campaign_id: str,
        *,
        state: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
        exclude_nodes: Collection[str] = (),
        only_nodes: Collection[str] | None = None,
    ) -> list[RunRow]:
        """Runs in ``seq`` order, after ``after_seq`` (keyset paging), except
        those on ``exclude_nodes`` (the submitter's passed-over nodes)."""
        limit = max(1, min(limit, MAX_LIST))
        query = (
            select(BenchRun)
            .where(BenchRun.campaign_id == campaign_id, BenchRun.seq > after_seq)
            .order_by(BenchRun.seq)
            .limit(limit)
        )
        if state is not None:
            query = query.where(BenchRun.state == state)
        if exclude_nodes:
            query = query.where(BenchRun.node_id.not_in(sorted(exclude_nodes)))
        if only_nodes is not None:  # V0.9 G5: the runner's per-node budget
            query = query.where(BenchRun.node_id.in_(sorted(only_nodes)))
        async with self._sessions() as db:
            return [_run(r) for r in await db.scalars(query)]

    @_typed
    async def run_by_job(self, job_ids: Collection[str]) -> dict[str, dict[str, Any]]:
        """:func:`campaign_identities` in its own session (the job API's
        ``benchmark`` field)."""
        async with self._sessions() as db:
            return await campaign_identities(db, job_ids)

    @_typed
    async def mark_submitted(
        self, run_id: str, job_id: str, *, generation: int | None = None
    ) -> bool:
        """``planned -> submitted`` with ``job_id``; False = the run had already
        moved on (another submit won). ``generation`` (G5): only while the run
        is still at that generation (the job's key was built from it)."""
        async with self._sessions() as db, db.begin():
            done = await db.execute(
                update(BenchRun)
                .where(
                    BenchRun.id == run_id,
                    BenchRun.state == "planned",
                    *(
                        ()
                        if generation is None
                        else (BenchRun.generation == generation,)
                    ),
                )
                .values(state="submitted", job_id=job_id, submitted_at=func.now())
                .execution_options(synchronize_session=False)
            )
            return bool(getattr(done, "rowcount", 0))

    @_typed
    async def mark_skipped(self, run_id: str, detail: str) -> bool:
        async with self._sessions() as db, db.begin():
            done = await db.execute(
                update(BenchRun)
                .where(BenchRun.id == run_id, BenchRun.state == "planned")
                .values(
                    state="skipped",
                    detail=detail[:MAX_RUN_DETAIL_CHARS],
                    ended_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
            return bool(getattr(done, "rowcount", 0))

    @_typed
    async def sync(self, campaign_id: str | None = None) -> int:
        """Link ``planned`` runs whose job exists (:meth:`_link_orphans`), move
        ``submitted`` runs whose job ended to ``done`` (job succeeded) or
        ``failed`` - ``ended_at`` = the job's **final transition** time from its
        history (G4 item 0.7, D239.3; ``Job.updated_at`` also moves when a
        terminal job is touched later, e.g. a late cancel request), else
        ``updated_at`` for a job with no history row - and close campaigns
        with no open run. The runs moved to ``done`` / ``failed``."""
        async with self._sessions() as db, db.begin():
            await self._link_orphans(db, campaign_id)
            settled = await self._settle_runner_runs(db, campaign_id)
            final_at = (
                select(JobTransition.at)
                .where(JobTransition.job_id == Job.id)
                .order_by(JobTransition.seq.desc())
                .limit(1)
                .scalar_subquery()
            )
            moved = await db.execute(
                update(BenchRun)
                .where(
                    BenchRun.state == "submitted",
                    BenchRun.job_id == Job.id,
                    Job.state.in_(_TERMINAL),
                    # G3 manual campaigns; a runner-owned one was settled above
                    BenchCampaign.id == BenchRun.campaign_id,
                    BenchCampaign.started_at.is_(None),
                    *(
                        ()
                        if campaign_id is None
                        else (BenchRun.campaign_id == campaign_id,)
                    ),
                )
                .values(
                    state=case(
                        (Job.state == JobState.SUCCEEDED.value, "done"), else_="failed"
                    ),
                    detail=case(
                        (Job.state == JobState.SUCCEEDED.value, None),
                        else_=func.left(
                            func.concat(
                                "job ",
                                Job.state,
                                ": ",
                                func.coalesce(Job.failure_type, ""),
                            ),
                            MAX_RUN_DETAIL_CHARS,
                        ),
                    ),
                    ended_at=func.coalesce(final_at, Job.updated_at),
                )
                .execution_options(synchronize_session=False)
            )
            count = int(getattr(moved, "rowcount", 0) or 0) + settled
            still_open = (
                select(BenchRun.id)
                .where(
                    BenchRun.campaign_id == BenchCampaign.id,
                    BenchRun.state.in_(_OPEN_RUN_STATES),
                )
                .exists()
            )
            await db.execute(
                update(BenchCampaign)
                .where(
                    BenchCampaign.state.in_(("active", "paused")),
                    ~still_open,
                    *(
                        ()
                        if campaign_id is None
                        else (BenchCampaign.id == campaign_id,)
                    ),
                )
                .values(
                    state="done",
                    finished_reason=case(
                        (
                            BenchCampaign.started_at.is_(None),
                            BenchCampaign.finished_reason,
                        ),
                        else_=func.coalesce(BenchCampaign.finished_reason, "complete"),
                    ),
                )
                .execution_options(synchronize_session=False)
            )
        if count:
            log.info(
                "controller.bench.runs_ended", count=count, campaign_id=campaign_id
            )
        return count

    async def _settle_runner_runs(
        self, db: AsyncSession, campaign_id: str | None
    ) -> int:
        """V0.9 G5: finished runs of runner-owned campaigns, by
        :func:`.outcome.settle` (rows locked; a concurrent sync skips them).
        The runs moved."""
        final_at = (
            select(JobTransition.at)
            .where(JobTransition.job_id == Job.id)
            .order_by(JobTransition.seq.desc())
            .limit(1)
            .scalar_subquery()
        )
        rows = list(
            (
                await db.execute(
                    select(
                        BenchRun.id,
                        BenchRun.job_id,
                        BenchRun.tries,
                        BenchRun.generation,
                        Job.state,
                        Job.failure_type,
                        func.coalesce(final_at, Job.updated_at),
                        BenchCampaign.max_infra_retries,
                        BenchCampaign.finished_reason,
                    )
                    .join(Job, Job.id == BenchRun.job_id)
                    .join(BenchCampaign, BenchCampaign.id == BenchRun.campaign_id)
                    .where(
                        BenchRun.state == "submitted",
                        Job.state.in_(_TERMINAL),
                        BenchCampaign.started_at.is_not(None),
                        *(
                            ()
                            if campaign_id is None
                            else (BenchRun.campaign_id == campaign_id,)
                        ),
                    )
                    .order_by(BenchRun.id)
                    .limit(_SETTLE_BATCH)
                    .with_for_update(of=BenchRun, skip_locked=True)
                )
            ).tuples()
        )
        moved = 0
        for (
            run_id,
            job_id,
            tries,
            generation,
            state,
            ftype,
            ended,
            retries,
            fin,
        ) in rows:
            out = settle(
                state,
                ftype,
                tries=int(tries),
                max_infra_retries=int(retries),
                stopped=fin is not None,
            )
            values: dict[str, Any] = {
                "state": out.state,
                "tries": out.tries,
                "last_failure": None
                if out.last_failure is None
                else out.last_failure[:MAX_LAST_FAILURE_CHARS],
                "detail": None
                if out.detail is None
                else out.detail[:MAX_RUN_DETAIL_CHARS],
                "ended_at": ended,
            }
            if out.requeued:
                values |= {"job_id": None, "generation": int(generation) + 1}
            done = await db.execute(
                update(BenchRun)
                .where(
                    BenchRun.id == run_id,
                    BenchRun.state == "submitted",
                    BenchRun.job_id == job_id,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if getattr(done, "rowcount", 0):
                moved += 1
                log.info(
                    "controller.bench.run_settled",
                    run_id=run_id,
                    job_id=job_id,
                    state=out.state,
                    tries=out.tries,
                    failure_type=ftype,
                    generation=int(generation) + (1 if out.requeued else 0),
                )
        return moved

    async def _link_orphans(self, db: AsyncSession, campaign_id: str | None) -> int:
        """G4 item 0.6 (D239.2): a crash between the job's commit and the run's
        ``planned -> submitted`` link leaves a ``planned`` run whose job exists
        under its ``bench:<run_id>`` Idempotency-Key; link it (``submitted_at``
        = the job's ``created_at``) so the run is not left planned - or tried
        again - and its job counts. Only a job that **is** that run's is linked:
        same key, owned by the campaign's creator, pinned to the run's node
        and submitted from its origin (a user cannot plant a job on a run). One
        ``UPDATE ... FROM job`` (the key is unique-indexed)."""
        linked = await db.execute(
            update(BenchRun)
            .where(
                BenchRun.state == "planned",
                BenchRun.job_id.is_(None),
                # this generation's key (G5: bench:<run_id>[:<generation>])
                Job.idempotency_key
                == case(
                    (
                        BenchRun.generation == 0,
                        func.concat(BENCH_KEY_PREFIX, BenchRun.id),
                    ),
                    else_=func.concat(
                        BENCH_KEY_PREFIX,
                        BenchRun.id,
                        ":",
                        cast(BenchRun.generation, String),
                    ),
                ),
                BenchCampaign.id == BenchRun.campaign_id,
                Job.owner_user_id.is_not_distinct_from(BenchCampaign.created_by),
                Job.submitter_node_id == BenchRun.origin_node_id,
                Job.placement["node_id"].astext == BenchRun.node_id,
                *(
                    ()
                    if campaign_id is None
                    else (BenchRun.campaign_id == campaign_id,)
                ),
            )
            .values(state="submitted", job_id=Job.id, submitted_at=Job.created_at)
            .execution_options(synchronize_session=False)
        )
        count = int(getattr(linked, "rowcount", 0) or 0)
        if count:
            log.warning(
                "controller.bench.orphan_jobs_linked",
                count=count,
                campaign_id=campaign_id,
            )
        return count
