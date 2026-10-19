"""Campaign controls and the runner's reads (V0.9 G5, roadmap 0.9.7, D219/D246; migration 0024).

The campaign state machine, all in PostgreSQL (so a controller ``kill -9``
loses nothing - the runner reads it again on its next tick):

``active`` (created; G3's manual ``submit`` drives it) --start--> ``active`` +
``started_at`` (**the runner owns it**; manual submit is refused
``409 campaign_running``) --pause--> ``paused`` --resume--> ``active``;
``cancel`` from any open state --> ``cancelled`` (its open runs ``skipped``
``cancelled``, the caller cancels the jobs :meth:`CampaignControl.cancel`
returns); no open run left --> ``done`` (``finished_reason`` ``complete``, or
``budget`` once :meth:`CampaignControl.stop_for_budget` skipped the rest).
Every control is idempotent (the same request twice = the same state);
a control a finished campaign cannot take is :class:`CampaignStateError`
(``conflict``).

The runner's reads (:meth:`runner_campaigns`, :meth:`node_slots` = the
node load and the last end in one snapshot, :meth:`user_busy_nodes`) and the status
numbers (:meth:`status`) use the database's clock, never the controller's -
elapsed times survive a restart and a wall-clock step.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import Float, cast, func, or_, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.bench.quality import FailureCounts, infra_failure_rate
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.states import ATTEMPT_TERMINAL_STATES, JOB_TERMINAL_STATES
from meshforge.controller.bench.keys import KEY_PREFIX
from meshforge.controller.bench.store import _typed
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.bench import (
    MAX_RUN_DETAIL_CHARS,
    BenchCampaign,
    BenchRun,
)
from meshforge.controller.store.models.job import Attempt, Job, JobTransition
from meshforge.controller.store.models.telemetry import RunRecord

__all__ = [
    "CampaignControl",
    "CampaignStateError",
    "CampaignStatus",
    "RunnerCampaign",
    "Throttle",
    "record_failure_counts",
]

_JOB_TERMINAL: Final = tuple(sorted(s.value for s in JOB_TERMINAL_STATES))
_ATTEMPT_TERMINAL: Final = tuple(sorted(s.value for s in ATTEMPT_TERMINAL_STATES))
_OPEN: Final = ("planned", "submitted")
_KEY_LIKE: Final = f"{KEY_PREFIX}%"
OWNER_PREEMPTED: Final = FailureType.OWNER_PREEMPTED.value


class CampaignStateError(MeshForgeError):
    """The campaign is in a state this control cannot take (``conflict``)."""

    default_failure_type = FailureType.CONFLICT


@dataclass(frozen=True, slots=True)
class Throttle:
    """Optional overrides given to ``start`` (None = keep)."""

    max_concurrent_per_node: int | None = None
    cooldown_s: float | None = None
    max_infra_retries: int | None = None


@dataclass(frozen=True, slots=True)
class RunnerCampaign:
    """One campaign the runner drives this tick."""

    id: str
    spec_yaml: str
    max_concurrent_per_node: int
    cooldown_s: float
    max_infra_retries: int
    elapsed_s: float
    """Database time since ``started_at``."""
    created_by: str | None


@dataclass(frozen=True, slots=True)
class CampaignStatus:
    counts: Mapping[str, int]
    done_percent: float
    median_run_s: float | None
    eta_s: float | None
    infra_failure_rate: float | None
    """Infrastructure failures / attempts of the campaign's run records,
    **excluding** ``owner_preempted`` (D246; criterion 1)."""
    records: int
    infra_failures: int
    preempted: int
    retries: int
    nodes_open: int
    extra: Mapping[str, Any] = field(default_factory=dict)


def _bench_key() -> Any:
    return Job.idempotency_key.like(_KEY_LIKE)


def _not_bench() -> Any:
    return or_(Job.idempotency_key.is_(None), ~Job.idempotency_key.like(_KEY_LIKE))


async def _node_load(db: AsyncSession, node_ids: Collection[str]) -> dict[str, int]:
    node = Job.placement["node_id"].astext
    rows = await db.execute(
        select(node, func.count())
        .where(
            _bench_key(),
            Job.state.not_in(_JOB_TERMINAL),
            node.in_(sorted(node_ids)),
        )
        .group_by(node)
    )
    return {str(n): int(c) for n, c in rows}


async def _last_end(
    db: AsyncSession, campaign_id: str, node_ids: Collection[str]
) -> dict[str, float]:
    """Seconds (database clock) since the campaign's last run on each node
    ended. A run ended when ``bench_run.ended_at`` is set (not ``skipped``)
    **or** - G5c item 0.0 (D255) - when it is still ``submitted`` but its job
    is already terminal (``sync`` has not settled it yet): that end is the
    job's final transition time, exactly what ``sync`` will stamp. Without the
    second source a job that ended after the tick's ``sync`` left its node
    free (the load no longer counts it) with no end to cool down from."""
    nodes = sorted(node_ids)
    final_at = (
        select(JobTransition.at)
        .where(JobTransition.job_id == Job.id)
        .order_by(JobTransition.seq.desc())
        .limit(1)
        .scalar_subquery()
    )
    ends = union_all(
        select(BenchRun.node_id.label("node"), BenchRun.ended_at.label("at")).where(
            BenchRun.campaign_id == campaign_id,
            BenchRun.node_id.in_(nodes),
            BenchRun.ended_at.is_not(None),
            BenchRun.state != "skipped",
        ),
        select(
            BenchRun.node_id.label("node"),
            func.coalesce(final_at, Job.updated_at).label("at"),
        )
        .join(Job, Job.id == BenchRun.job_id)
        .where(
            BenchRun.campaign_id == campaign_id,
            BenchRun.node_id.in_(nodes),
            BenchRun.state == "submitted",
            Job.state.in_(_JOB_TERMINAL),
        ),
    ).subquery()
    rows = await db.execute(
        select(
            ends.c.node, func.extract("epoch", func.now() - func.max(ends.c.at))
        ).group_by(ends.c.node)
    )
    return {str(n): float(s) for n, s in rows if s is not None}


async def record_failure_counts(db: AsyncSession, campaign_id: str) -> FailureCounts:
    """G5c (0.9.8, D254): the counts behind criterion 1's infrastructure
    failure rate (:func:`meshforge.bench.quality.infra_failure_rate`) - **one**
    query for ``compute bench status`` and the quality report. Over the
    campaign's run records without a ``bench_role`` (fillers / warm-ups never
    count, D244): all of them, infrastructure failures **except**
    ``owner_preempted`` (D246), and the pre-emptions."""
    not_role = RunRecord.condition_measured["bench_role"].astext.is_(None)
    total, infra, preempted = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(
                    RunRecord.failure_class == "infrastructure",
                    RunRecord.failure_type.is_distinct_from(OWNER_PREEMPTED),
                ),
                func.count().filter(RunRecord.failure_type == OWNER_PREEMPTED),
            ).where(RunRecord.campaign_id == campaign_id, not_role)
        )
    ).one()
    return FailureCounts(records=int(total), infra=int(infra), preempted=int(preempted))


class CampaignControl:
    """See module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    # ------------------------------------------------------------ controls

    async def _state(self, db: AsyncSession, campaign_id: str) -> str | None:
        state = await db.scalar(
            select(BenchCampaign.state)
            .where(BenchCampaign.id == campaign_id)
            .with_for_update()
        )
        return None if state is None else str(state)

    @_typed
    async def start(self, campaign_id: str, throttle: Throttle | None = None) -> bool:
        """The runner owns the campaign from now (idempotent; also resumes a
        paused one). False = no such campaign."""
        values: dict[str, Any] = {
            "state": "active",
            "started_at": func.coalesce(BenchCampaign.started_at, func.now()),
        }
        if throttle is not None:
            for name in ("max_concurrent_per_node", "cooldown_s", "max_infra_retries"):
                value = getattr(throttle, name)
                if value is not None:
                    values[name] = value
        return await self._move(campaign_id, ("active", "paused"), values, "start")

    @_typed
    async def pause(self, campaign_id: str) -> bool:
        """``active -> paused``: nothing new is submitted; running jobs finish."""
        return await self._move(
            campaign_id, ("active", "paused"), {"state": "paused"}, "pause"
        )

    @_typed
    async def resume(self, campaign_id: str) -> bool:
        """``paused -> active`` (the runner owns it from now, like ``start``)."""
        return await self.start(campaign_id)

    async def _move(
        self,
        campaign_id: str,
        allowed: tuple[str, ...],
        values: Mapping[str, Any],
        verb: str,
    ) -> bool:
        async with self._sessions() as db, db.begin():
            state = await self._state(db, campaign_id)
            if state is None:
                return False
            if state not in allowed:
                raise CampaignStateError(
                    f"the campaign is {state}; it cannot {verb}",
                    details={"state": state},
                )
            await db.execute(
                update(BenchCampaign)
                .where(BenchCampaign.id == campaign_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            return True

    @_typed
    async def cancel(self, campaign_id: str) -> list[str] | None:
        """``cancelled``: open runs ``skipped`` (``cancelled``). Returns the
        campaign's **non-terminal benchmark jobs** (run jobs of every
        generation, fillers, warm-ups) for the caller to cancel - None = no
        such campaign. Idempotent: a cancelled campaign returns what is still
        running. A ``done`` campaign cannot be cancelled."""
        async with self._sessions() as db, db.begin():
            state = await self._state(db, campaign_id)
            if state is None:
                return None
            if state == "done":
                raise CampaignStateError(
                    "the campaign is done; nothing to cancel", details={"state": state}
                )
            run_of_job = func.split_part(
                func.substr(Job.idempotency_key, len(KEY_PREFIX) + 1), ":", 1
            )
            jobs = list(
                await db.scalars(
                    select(Job.id)
                    .where(
                        _bench_key(),
                        Job.state.not_in(_JOB_TERMINAL),
                        run_of_job.in_(
                            select(BenchRun.id).where(
                                BenchRun.campaign_id == campaign_id
                            )
                        ),
                    )
                    .order_by(Job.id)
                )
            )
            await db.execute(
                update(BenchRun)
                .where(BenchRun.campaign_id == campaign_id, BenchRun.state.in_(_OPEN))
                .values(state="skipped", detail="cancelled", ended_at=func.now())
                .execution_options(synchronize_session=False)
            )
            await db.execute(
                update(BenchCampaign)
                .where(BenchCampaign.id == campaign_id)
                .values(state="cancelled", finished_reason="cancelled")
                .execution_options(synchronize_session=False)
            )
            return [str(j) for j in jobs]

    @_typed
    async def stop_for_budget(self, campaign_id: str, detail: str) -> int:
        """``max_hours`` passed: no more submissions - the planned runs are
        ``skipped``, ``finished_reason = budget`` (the campaign is ``done``
        once its submitted runs end). The runs skipped."""
        async with self._sessions() as db, db.begin():
            moved = await db.execute(
                update(BenchRun)
                .where(BenchRun.campaign_id == campaign_id, BenchRun.state == "planned")
                .values(
                    state="skipped",
                    detail=detail[:MAX_RUN_DETAIL_CHARS],
                    ended_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
            await db.execute(
                update(BenchCampaign)
                .where(
                    BenchCampaign.id == campaign_id,
                    BenchCampaign.finished_reason.is_(None),
                )
                .values(finished_reason="budget")
                .execution_options(synchronize_session=False)
            )
            return int(getattr(moved, "rowcount", 0) or 0)

    @contextlib.asynccontextmanager
    async def tick_lock(self, key: int) -> AsyncIterator[bool]:
        """G5c item 0.5 (D252.1, D254): one runner tick at a time per
        database. Yields whether this caller got PostgreSQL's
        **transaction-scoped** advisory lock ``key``
        (``pg_try_advisory_xact_lock``: never waits); the transaction - and so
        the lock - is held until the block exits, and a crashed controller's
        lock goes with its connection. A caller that did not get it must skip
        its tick: two runners on one database would otherwise both see a
        node's slot free and fill it twice."""
        async with self._sessions() as db, db.begin():
            got = await db.scalar(select(func.pg_try_advisory_xact_lock(key)))
            yield bool(got)

    # ------------------------------------------------------------ runner reads

    @_typed
    async def runner_campaigns(self) -> list[RunnerCampaign]:
        """Started, ``active``, still submitting (oldest first)."""
        async with self._sessions() as db:
            rows = await db.execute(
                select(
                    BenchCampaign.id,
                    BenchCampaign.spec_yaml,
                    BenchCampaign.max_concurrent_per_node,
                    BenchCampaign.cooldown_s,
                    BenchCampaign.max_infra_retries,
                    func.extract("epoch", func.now() - BenchCampaign.started_at),
                    BenchCampaign.created_by,
                )
                .where(
                    BenchCampaign.state == "active",
                    BenchCampaign.started_at.is_not(None),
                    BenchCampaign.finished_reason.is_(None),
                )
                .order_by(BenchCampaign.started_at, BenchCampaign.id)
            )
            return [
                RunnerCampaign(
                    id=r[0],
                    spec_yaml=r[1],
                    max_concurrent_per_node=int(r[2]),
                    cooldown_s=float(r[3]),
                    max_infra_retries=int(r[4]),
                    elapsed_s=max(0.0, float(r[5] or 0.0)),
                    created_by=r[6],
                )
                for r in rows
            ]

    @_typed
    async def planned_nodes(self, campaign_id: str) -> set[str]:
        """Nodes that still have a planned run in the campaign."""
        async with self._sessions() as db:
            return {
                str(n)
                for n in await db.scalars(
                    select(BenchRun.node_id)
                    .where(
                        BenchRun.campaign_id == campaign_id,
                        BenchRun.state == "planned",
                    )
                    .distinct()
                )
            }

    @_typed
    async def node_bench_load(self, node_ids: Collection[str]) -> dict[str, int]:
        """Non-terminal benchmark jobs (any campaign, fillers included) pinned
        to each node."""
        if not node_ids:
            return {}
        async with self._sessions() as db:
            return await _node_load(db, node_ids)

    @_typed
    async def seconds_since_last_end(
        self, campaign_id: str, node_ids: Collection[str]
    ) -> dict[str, float]:
        """Database seconds since the campaign's last run on each node ended
        (absent = none ended yet) - see :func:`_last_end`."""
        if not node_ids:
            return {}
        async with self._sessions() as db:
            return await _last_end(db, campaign_id, node_ids)

    @_typed
    async def node_slots(
        self, campaign_id: str, node_ids: Collection[str]
    ) -> tuple[dict[str, int], dict[str, float]]:
        """G5c item 0.0 (D255): :meth:`node_bench_load` **and**
        :meth:`seconds_since_last_end` read in **one** ``REPEATABLE READ``
        snapshot, so a job that ends while the runner reads is seen either
        still running (it fills the slot) or ended (cool-down) - never
        neither. (Two ``READ COMMITTED`` statements could see the job
        terminal in the load read and not yet ended in the cool-down read.)"""
        if not node_ids:
            return {}, {}
        async with self._sessions() as db:
            await db.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            load = await _node_load(db, node_ids)
            since = await _last_end(db, campaign_id, node_ids)
            await db.rollback()  # read-only
        return load, since

    @_typed
    async def user_busy_nodes(self) -> set[str]:
        """Checklist 30: nodes with a **user** job (not a benchmark) running
        there now, or queued pinned there - the runner submits nothing to
        them, so a benchmark never competes with a user's job."""
        async with self._sessions() as db:
            running = set(
                await db.scalars(
                    select(Attempt.node_id)
                    .join(Job, Job.id == Attempt.job_id)
                    .where(Attempt.state.not_in(_ATTEMPT_TERMINAL), _not_bench())
                    .distinct()
                )
            )
            pinned = set(
                await db.scalars(
                    select(Job.placement["node_id"].astext)
                    .where(
                        Job.state.not_in(_JOB_TERMINAL),
                        _not_bench(),
                        Job.placement["node_id"].astext.is_not(None),
                    )
                    .distinct()
                )
            )
        return {str(n) for n in running | pinned if n}

    # ------------------------------------------------------------ status

    @_typed
    async def status(
        self, campaign_id: str, counts: Mapping[str, int], *, per_node: int = 1
    ) -> CampaignStatus:
        """``compute bench status`` numbers (see :class:`CampaignStatus`)."""
        async with self._sessions() as db:
            median = await db.scalar(
                select(
                    func.percentile_cont(0.5).within_group(
                        cast(
                            func.extract(
                                "epoch", BenchRun.ended_at - BenchRun.submitted_at
                            ),
                            Float,
                        )
                    )
                ).where(
                    BenchRun.campaign_id == campaign_id,
                    BenchRun.state == "done",
                    BenchRun.ended_at.is_not(None),
                    BenchRun.submitted_at.is_not(None),
                )
            )
            failures = await record_failure_counts(db, campaign_id)
            retries = await db.scalar(
                select(func.coalesce(func.sum(BenchRun.tries), 0)).where(
                    BenchRun.campaign_id == campaign_id
                )
            )
            nodes_open = await db.scalar(
                select(func.count(func.distinct(BenchRun.node_id))).where(
                    BenchRun.campaign_id == campaign_id, BenchRun.state.in_(_OPEN)
                )
            )
        all_runs = sum(int(v) for v in counts.values())
        open_runs = int(counts.get("planned", 0)) + int(counts.get("submitted", 0))
        finished = all_runs - open_runs
        median_s = None if median is None else float(median)
        lanes = max(1, int(nodes_open or 0) * max(1, per_node))
        return CampaignStatus(
            counts=dict(counts),
            done_percent=round(100.0 * finished / all_runs, 1) if all_runs else 0.0,
            median_run_s=None if median_s is None else round(median_s, 3),
            eta_s=None if median_s is None else round(open_runs * median_s / lanes, 1),
            infra_failure_rate=infra_failure_rate(failures),
            records=failures.records,
            infra_failures=failures.infra,
            preempted=failures.preempted,
            retries=int(retries or 0),
            nodes_open=int(nodes_open or 0),
        )
