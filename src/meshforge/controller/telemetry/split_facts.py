"""What a split job's run records add (telemetry schema v3, V0.10 G8, D271/D274).

Every record of a split job - its lanes and its combine - carries the job's
**first plan** (the planner's first dispatched decision: mode, node set, chunk
size and count, predicted makespan) and its **strategy**: ``mode3`` when that
plan split, else ``local`` / ``mode1`` by its one lane's node ("don't split" is
the same lane path, D269). The plan is the job's, not the attempt's: a re-plan
after every lane was lost (D267e) is visible in the chunk records, not here.

The **job row** is the record of the combine attempt that ended the job
``DONE`` (it produced the job's output). It alone carries the measured job
numbers, read when the recorder builds it (the job has ended, every chunk
record is written):

* ``split_makespan_ms`` - first ``running`` arrow -> the job's end (D274 C2);
* ``split_job_ms`` - first ``preparing`` arrow (lanes dispatched) -> the end:
  lane staging + env, every chunk, the combine - what the split job cost,
  the labels' measure for a split family (``meshforge.bench.records``);
* ``split_combine_ms`` - ``job.split_combine_ms`` (the combine's run phase);
* ``split_ideal_bound_s`` - :func:`ideal_bound_s`.

Both job times are differences of two controller timestamps (one clock, the
controller's; not the agents') - they include the network and the dispatch
ticks, which is the point. :func:`split_facts` reads everything for a batch of
attempts in a bounded number of statements (one per kind, never per job).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.states import AttemptRole, AttemptState
from meshforge.controller.store.models.job import (
    Attempt,
    AttemptPhaseRow,
    Job,
    JobTransition,
)
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.telemetry import ChunkRecord

__all__ = [
    "PLAN_STRATEGY",
    "JobTimes",
    "SplitFacts",
    "chosen_row",
    "ideal_bound_s",
    "split_facts",
]

PLAN_STRATEGY: Final[dict[str, str]] = {
    "split": "mode3",
    "local": "local",
    "offload": "mode1",
}
"""A plan row's ``mode`` -> the record's strategy."""
_ROUND: Final = 6


@dataclass(frozen=True, slots=True)
class JobTimes:
    """The job row's measured numbers (module docstring)."""

    makespan_ms: int | None = None
    job_ms: int | None = None
    combine_ms: int | None = None
    ideal_bound_s: float | None = None


@dataclass(frozen=True, slots=True)
class SplitFacts:
    """One split job as its records see it."""

    items: int | None
    mode: str | None
    """The first plan's chosen row ``mode`` (``split`` / ``local`` /
    ``offload``); None = no dispatched decision was found."""
    node_set: tuple[str, ...] = ()
    chunk_items: int | None = None
    chunks: int | None = None
    predicted_s: float | None = None
    decision_policy: str | None = None
    decision_chosen: str | None = None
    decision_candidates: Sequence[Mapping[str, Any]] = ()
    decision_shadow: str | None = None
    decision_shadow_set: tuple[str, ...] = ()
    times: Mapping[str, JobTimes] = field(default_factory=dict)
    """attempt id of the job row -> its numbers (only the combine that ended
    the job DONE)."""

    @property
    def strategy(self) -> str | None:
        return None if self.mode is None else PLAN_STRATEGY.get(self.mode)


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def _active(row: Mapping[str, Any]) -> tuple[str, ...]:
    """A plan row's lanes: its node set without the ``inactive`` nodes (those
    predicted to get no chunk; the dispatcher creates no lane for them)."""
    inactive = set(_ids(row.get("inactive")))
    return tuple(n for n in _ids(row.get("node_set")) if n not in inactive)


def chosen_row(candidates: object) -> Mapping[str, Any] | None:
    """The rank-1 row of a stored decision's candidates (the chosen one)."""
    if not isinstance(candidates, list):
        return None
    for row in candidates:
        if isinstance(row, Mapping) and row.get("rank") == 1:
            return row
    return None


def ideal_bound_s(
    items: int | None,
    rates: Iterable[tuple[int, int]],
    lane_start_ms: Iterable[int],
    combine_ms: int | None,
) -> float | None:
    """D274's ideal makespan, from this job's own measurements: ``items`` over
    the sum of the lanes' compute rates (each node's committed items over the
    run time of its committed chunks - steady state, staging and upload
    excluded) **plus** the slowest lane's stage + env **plus** the combine's
    run. None when nothing was measured (no rate, no combine time). Pure."""
    if items is None or items <= 0 or combine_ms is None:
        return None
    total_rate = 0.0
    for done, run_ms in rates:
        if done > 0 and run_ms > 0:
            total_rate += done / (run_ms / 1000.0)
    if total_rate <= 0:
        return None
    start_s = max((s for s in lane_start_ms if s >= 0), default=0) / 1000.0
    bound = items / total_rate + start_s + combine_ms / 1000.0
    return round(bound, _ROUND) if math.isfinite(bound) else None


def _span_ms(start: dt.datetime | None, end: dt.datetime | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds() * 1000)


async def _first_decisions(
    db: AsyncSession, job_ids: Collection[str]
) -> dict[str, PlanDecision]:
    out: dict[str, PlanDecision] = {}
    for decision in await db.scalars(
        select(PlanDecision)
        .where(
            PlanDecision.job_id.in_(sorted(job_ids)),
            PlanDecision.outcome == "dispatched",
        )
        .order_by(PlanDecision.job_id, PlanDecision.id)
    ):
        out.setdefault(decision.job_id, decision)
    return out


async def _job_times(
    db: AsyncSession, jobs: Mapping[str, Job], rows: Mapping[str, str]
) -> dict[str, JobTimes]:
    """``rows`` = job id -> attempt id of its job row."""
    if not rows:
        return {}
    ids = sorted(rows)
    arrows: dict[tuple[str, str], dt.datetime] = {}
    for job_id, to_state, at in (
        await db.execute(
            select(
                JobTransition.job_id, JobTransition.to_state, func.min(JobTransition.at)
            )
            .where(JobTransition.job_id.in_(ids))
            .group_by(JobTransition.job_id, JobTransition.to_state)
        )
    ).tuples():
        arrows[(str(job_id), str(to_state))] = at
    rates: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for job_id, done, run_ms in (
        await db.execute(
            select(
                ChunkRecord.job_id,
                func.sum(ChunkRecord.items),
                func.sum(ChunkRecord.run_ms),
            )
            .where(
                ChunkRecord.job_id.in_(ids),
                ChunkRecord.won.is_(True),
                ChunkRecord.run_ms.is_not(None),
            )
            .group_by(ChunkRecord.job_id, ChunkRecord.node_id)
        )
    ).tuples():
        rates[str(job_id)].append((int(done or 0), int(run_ms or 0)))
    starts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for job_id, attempt_id, duration in (
        await db.execute(
            select(Attempt.job_id, Attempt.id, AttemptPhaseRow.duration_ms)
            .join(AttemptPhaseRow, AttemptPhaseRow.attempt_id == Attempt.id)
            .where(
                and_(
                    Attempt.job_id.in_(ids),
                    Attempt.role == AttemptRole.LANE.value,
                    AttemptPhaseRow.phase.in_(("stage", "env")),
                    AttemptPhaseRow.skipped.is_(False),
                )
            )
        )
    ).tuples():
        starts[str(job_id)][str(attempt_id)] += int(duration or 0)
    out: dict[str, JobTimes] = {}
    for job_id, attempt_id in rows.items():
        job = jobs[job_id]
        end = arrows.get((job_id, "succeeded"))
        combine_ms = _int(job.split_combine_ms)
        out[attempt_id] = JobTimes(
            makespan_ms=_span_ms(arrows.get((job_id, "running")), end),
            job_ms=_span_ms(arrows.get((job_id, "preparing")), end),
            combine_ms=combine_ms,
            ideal_bound_s=ideal_bound_s(
                _int(job.split_item_count),
                rates.get(job_id, ()),
                starts.get(job_id, {}).values(),
                combine_ms,
            ),
        )
    return out


async def split_facts(
    db: AsyncSession, attempts: Sequence[Attempt], jobs: Mapping[str, Job]
) -> dict[str, SplitFacts]:
    """job id -> :class:`SplitFacts` for the split jobs among ``attempts``'
    jobs (module docstring); bounded statements per batch."""
    split_jobs = {j.id: j for j in jobs.values() if j.split is not None}
    if not split_jobs:
        return {}
    decisions = await _first_decisions(db, split_jobs)
    job_rows = {
        a.job_id: a.id
        for a in attempts
        if a.job_id in split_jobs
        and a.role == AttemptRole.COMBINE.value
        and a.state == AttemptState.DONE.value
    }
    times = await _job_times(db, split_jobs, job_rows)
    out: dict[str, SplitFacts] = {}
    for job_id, job in split_jobs.items():
        decision = decisions.get(job_id)
        chosen = None if decision is None else chosen_row(decision.candidates)
        estimate = {} if chosen is None else chosen.get("estimate")
        out[job_id] = SplitFacts(
            items=_int(job.split_item_count),
            mode=None if chosen is None else str(chosen.get("mode") or "") or None,
            node_set=() if chosen is None else _active(chosen),
            chunk_items=None if chosen is None else _int(chosen.get("chunk_items")),
            chunks=None if chosen is None else _int(chosen.get("chunks")),
            predicted_s=_float(estimate.get("total_s"))
            if isinstance(estimate, Mapping)
            else None,
            decision_policy=None if decision is None else decision.policy,
            decision_chosen=None if decision is None else decision.chosen_node_id,
            decision_candidates=()
            if decision is None or not isinstance(decision.candidates, list)
            else decision.candidates,
            decision_shadow=None
            if decision is None
            else decision.shadow_chosen_node_id,
            decision_shadow_set=()
            if decision is None
            else _ids(decision.shadow_node_set),
            times={a: t for a, t in times.items() if job_rows.get(job_id) == a},
        )
    return out
