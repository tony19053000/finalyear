"""The controller's side of benchmark conditions (V0.9 G4, roadmap 0.9.5, D217/D241).

A run's condition is the one canonical string in ``bench_run.conditions``.
The agent applies ``cpu_load`` / ``gpu_load`` / ``netem`` / ``cache``
(:mod:`meshforge.agent.conditions`); the controller

* sends it with the run's ``StartAttempt`` (:meth:`BenchConditions.agent_condition`,
  read by the run's ``bench:<run_id>`` Idempotency-Key - one primary-key read,
  only for benchmark jobs);
* applies its own kinds at submit (:mod:`.submit`): ``queue:K`` = K **filler**
  jobs (the run's own workload, lowest priority, pinned to the run's node,
  key ``bench:<run_id>:filler:<i>``) queued before the run; ``cache:warm`` =
  one unrecorded **warm-up** run of the same job first (key
  ``bench:<run_id>:warmup``); ``unavailable:T`` = the run's origin is
  **paused** for T s (:class:`NodePauses`: the planner sees it ``draining`` -
  nothing new is dispatched there, the shadow plan avoids it) right before
  the mode1 run is submitted. Fillers and warm-ups have no ``bench_run`` link,
  so they are never recorded as campaign runs (their records carry no
  campaign id), and their ``bench:`` key keeps them out of priority aging
  like every benchmark job (D237);
* derives, when the run's record is built, what it measured for its own
  kinds (:func:`bench_identities` - the recorder's campaign hook): fillers
  still queued or running when the run was queued, the warm-up's outcome,
  how long after the pause began the run was dispatched.

Nothing here writes a new table (lane B adds no migration, D241): the
controller-side facts are derived from the job rows themselves, so they
survive a controller restart - except a pause, which is in memory (a restart
ends it early; the record then shows the run dispatched after the pause).
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from meshforge.common.bench_conditions import ConditionError, parse_condition
from meshforge.common.logging import get_logger
from meshforge.common.states import JOB_TERMINAL_STATES
from meshforge.controller.bench.keys import (
    FILLER_TAG,
    KEY_PREFIX,
    WARMUP_TAG,
    filler_key,
    pre_job_role,
    run_id_of_key,
    run_key,
    run_of_key,
    warmup_key,
)
from meshforge.controller.bench.store import campaign_identities
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.bench import BenchCampaign, BenchRun
from meshforge.controller.store.models.job import Attempt, Job, JobTransition

__all__ = [
    "FILLER_TAG",
    "KEY_PREFIX",
    "WARMUP_TAG",
    "BenchConditions",
    "NodePauses",
    "bench_identities",
    "filler_key",
    "pre_job_role",
    "run_id_of_key",
    "run_key",
    "run_of_key",
    "warmup_key",
]

log = get_logger(__name__)

_TERMINAL: Final = tuple(sorted(s.value for s in JOB_TERMINAL_STATES))


async def _pre_job_identities(
    db: AsyncSession, job_ids: Collection[str]
) -> dict[str, dict[str, Any]]:
    """``job_id -> {bench_role, run_id}`` for the fillers / warm-ups among
    ``job_ids`` (fix round 1): their records carry the tag in
    ``condition_measured`` so label / training filters can drop them - they
    are system-made background jobs, not user demand nor campaign runs."""
    if not job_ids:
        return {}
    rows = await db.execute(
        select(Job.id, Job.idempotency_key).where(
            Job.id.in_(sorted(job_ids)),
            Job.idempotency_key.like(f"{KEY_PREFIX}%:%"),
        )
    )
    out: dict[str, dict[str, Any]] = {}
    for job_id, key in rows:
        role = pre_job_role(key)
        if role is not None:
            out[str(job_id)] = {"bench_role": role[0], "run_id": role[1]}
    return out


class NodePauses:
    """Nodes paused by ``unavailable:T`` runs (in memory, monotonic clock).
    The dispatch planner treats a paused node as ``draining``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}

    def pause(self, node_id: str, seconds: float) -> None:
        until = self._clock() + seconds
        if until > self._until.get(node_id, 0.0):
            self._until[node_id] = until
        log.info("controller.bench.node_paused", node_id=node_id, seconds=seconds)

    def paused(self, node_id: str) -> bool:
        until = self._until.get(node_id)
        if until is None:
            return False
        if self._clock() >= until:
            del self._until[node_id]
            return False
        return True


class BenchConditions:
    """What the dispatcher asks about a benchmark job's condition."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = create_session_factory(engine)

    async def agent_condition(self, idempotency_key: str | None) -> str | None:
        """The canonical condition the run job's agent must apply (None: not
        a run job, or a condition the controller applies itself)."""
        run_id = run_id_of_key(idempotency_key)
        if run_id is None:
            return None
        async with self._sessions() as db:
            conditions = await db.scalar(
                select(BenchRun.conditions).where(BenchRun.id == run_id)
            )
        if not conditions:
            return None
        try:
            condition = parse_condition(conditions[0])
        except ConditionError:  # pragma: no cover - stored from a parsed spec
            return None
        return condition.canonical() if condition.needs_agent else None


async def bench_identities(
    db: AsyncSession,
    job_ids: Collection[str],
    *,
    started_at: dt.datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """The recorder's campaign hook: :func:`~.store.campaign_identities` plus,
    per run job, its ``condition`` (canonical), the campaign's
    ``session_label`` and the controller-side ``condition_facts``.
    ``started_at`` = when this controller process started (wall clock): a
    pause (in memory) that a restart may have ended early is reported
    ``pause_lost``."""
    identities = await campaign_identities(db, job_ids)
    others = [j for j in job_ids if j not in identities]
    pre_jobs = await _pre_job_identities(db, others)
    if not identities:
        return pre_jobs
    run_ids = {str(i["run_id"]): job for job, i in identities.items()}
    rows = await db.execute(
        select(
            BenchRun.id,
            BenchRun.conditions,
            BenchRun.origin_node_id,
            BenchCampaign.summary,
        )
        .join(BenchCampaign, BenchCampaign.id == BenchRun.campaign_id)
        .where(BenchRun.id.in_(sorted(run_ids)))
    )
    for run_id, conditions, origin, summary in rows:
        identity = identities[run_ids[run_id]]
        condition = str(conditions[0]) if conditions else "none"
        identity["condition"] = condition
        label = (summary or {}).get("session_label")
        identity["session_label"] = label if isinstance(label, str) else None
        identity["origin_node_id"] = origin
    try:
        facts = await _facts(db, identities, started_at)
    except SQLAlchemyError:
        raise  # the sweep fails and is retried (recorder rule)
    except Exception:
        # a bug here must never jam the recorder: without facts the
        # controller-side conditions are unverified -> condition_failed,
        # never labelled wrong
        log.exception("controller.bench.condition_facts_failed")
        facts = {}
    for job_id, value in facts.items():
        identities[job_id]["condition_facts"] = value
    return {**identities, **pre_jobs}


async def _facts(
    db: AsyncSession,
    identities: Mapping[str, Mapping[str, Any]],
    started_at: dt.datetime | None,
) -> dict[str, dict[str, Any]]:
    wanted: dict[str, Any] = {}
    for job_id, identity in identities.items():
        try:
            condition = parse_condition(identity.get("condition", "none"))
        except ConditionError:  # pragma: no cover - stored from a parsed spec
            continue
        if condition.kind.value in ("queue", "cache", "unavailable"):
            wanted[job_id] = condition
    if not wanted:
        return {}
    # (a Result has .keys(): dict(result) would treat it as a mapping)
    created: dict[str, tuple[dt.datetime, int]] = {}
    for job_id, created_at, key in (
        await db.execute(
            select(Job.id, Job.created_at, Job.idempotency_key).where(
                Job.id.in_(sorted(wanted))
            )
        )
    ).tuples():
        found = run_of_key(key)
        created[job_id] = (created_at, 0 if found is None else found[1])
    out: dict[str, dict[str, Any]] = {}
    for job_id, condition in wanted.items():
        run_id = str(identities[job_id]["run_id"])
        if job_id not in created:
            continue
        queued_at, generation = created[job_id]
        kind = condition.kind.value
        if kind == "queue":
            count = int(getattr(condition, "jobs", 0))
            out[job_id] = await _queue_facts(
                db, run_id, count, queued_at, first=generation * count + 1
            )
        elif kind == "cache" and getattr(condition, "mode", "") == "warm":
            state = await db.scalar(
                select(Job.state).where(
                    Job.idempotency_key == warmup_key(run_id, generation)
                )
            )
            out[job_id] = {"warmup_state": state}
        elif kind == "unavailable":
            first = await db.scalar(
                select(func.min(Attempt.created_at)).where(Attempt.job_id == job_id)
            )
            out[job_id] = {
                "paused_node_id": identities[job_id].get("origin_node_id"),
                "seconds": getattr(condition, "seconds", None),
                "dispatched_after_s": None
                if first is None
                else round((first - queued_at).total_seconds(), 3),
                # this process started after the pause began and before the
                # run was dispatched: the in-memory pause may be gone
                "pause_lost": started_at is not None
                and first is not None
                and queued_at < started_at < first,
            }
    return out


async def _queue_facts(
    db: AsyncSession,
    run_id: str,
    count: int,
    queued_at: dt.datetime,
    *,
    first: int = 1,
) -> dict[str, Any]:
    """Fillers of ``run_id`` (this generation's: ``first .. first+count-1``)
    created before the run job and not yet ended when it was queued (= the
    queue in front of it). Exact keys, no LIKE."""
    keys = [filler_key(run_id, i) for i in range(first, first + count)]
    fillers = list(
        (
            await db.execute(
                select(Job.id, Job.created_at).where(Job.idempotency_key.in_(keys))
            )
        ).tuples()
    )
    ended: dict[str, dt.datetime] = dict(
        list(
            (
                await db.execute(
                    select(JobTransition.job_id, func.min(JobTransition.at))
                    .where(
                        JobTransition.job_id.in_([f[0] for f in fillers]),
                        JobTransition.to_state.in_(_TERMINAL),
                    )
                    .group_by(JobTransition.job_id)
                )
            ).tuples()
        )
    )
    ahead = sum(
        1
        for job_id, created_at in fillers
        if created_at <= queued_at
        and (ended.get(job_id) is None or ended[job_id] > queued_at)
    )
    return {"fillers": len(fillers), "fillers_ahead": ahead}
