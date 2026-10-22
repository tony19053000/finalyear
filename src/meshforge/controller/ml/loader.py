"""Read the ML dataset's runs from PostgreSQL (V0.15 G1, D333). I/O.

:func:`iter_runs` pages through **the calibration's own matched-run read**
(:func:`~meshforge.controller.calibration_store.read_outcomes`, keyset paged,
newest first): the same ``dispatched`` decisions joined to their ``done``
attempt, run record family and fingerprint, with their phases - so ML and
calibration are judged on identical pairs. Per page it adds, in a bounded
number of ``IN`` reads (never one per decision, never an unbounded
``fetchall``):

* each decision's ``job_id`` and **replay** (the feature source);
* for split jobs: the lane attempts of each decision's **round** - role
  ``lane`` attempts of the job whose fencing token is at least the decision's
  ``attempt_no`` and below the job's next dispatched decision's (a round's
  lanes get consecutive tokens in the dispatch transaction) - with their
  phases and ``chunk_record`` rows (items, run time, outcome);
* the job's ``done`` combine attempt (newest), attached to the job's **last**
  dispatched decision only (a combine follows the final round).

At most ``max_runs`` decisions are read (``[controller.planner]
ml_dataset_max_runs``); a page's runs are handed to the caller and dropped,
so the replays of one page (at most ``page_size x replay_max_bytes``) are the
most held at once.

V0.15 G2: :func:`read_watermark` (D355) - the cheap key the dataset route
caches on - and :func:`count_node_failures` (D354) - the failure events a
failure classifier could learn from, by the risk term's own definition.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.controller.calibration_store import matched_watermark, read_outcomes
from meshforge.controller.jobs.queue_reads import node_caused_end
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.targets import (
    ChunkOutcome,
    CombineOutcome,
    LaneOutcome,
    RunInput,
)
from meshforge.controller.planner.calibration import DecisionOutcome, chosen_row
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.job import Attempt, AttemptPhaseRow
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.telemetry import ChunkRecord, RunRecord

__all__ = [
    "DEFAULT_MAX_RUNS",
    "PAGE_SIZE",
    "MlLoadError",
    "Page",
    "Watermark",
    "count_node_failures",
    "iter_runs",
    "read_watermark",
]

DEFAULT_MAX_RUNS: Final = 50_000
PAGE_SIZE: Final = 500
_IN_BATCH: Final = 5_000
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)


class MlLoadError(MeshForgeError):
    """The database refused or lost the dataset read (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class Page:
    runs: list[RunInput]
    truncated: bool = False
    """The cap stopped the read while older runs were left."""


def _batches(items: Sequence[str]) -> list[Sequence[str]]:
    return [items[i : i + _IN_BATCH] for i in range(0, len(items), _IN_BATCH)]


async def _phases(
    db: AsyncSession, ids: Sequence[str]
) -> dict[str, dict[str, int | None]]:
    out: dict[str, dict[str, int | None]] = {}
    for batch in _batches(sorted(set(ids))):
        for phase in await db.scalars(
            select(AttemptPhaseRow).where(AttemptPhaseRow.attempt_id.in_(batch))
        ):
            out.setdefault(phase.attempt_id, {})[phase.phase] = (
                None if phase.skipped else int(phase.duration_ms)
            )
    return out


def _is_split(outcome: DecisionOutcome) -> bool:
    row = chosen_row(outcome)
    return row is not None and bool(row.get("node_set"))


@dataclass(frozen=True, slots=True)
class _Decision:
    job_id: str
    attempt_no: int
    replay: Mapping[str, Any] | None


async def _decisions(db: AsyncSession, ids: Sequence[int]) -> dict[int, _Decision]:
    out: dict[int, _Decision] = {}
    for start in range(0, len(ids), _IN_BATCH):
        batch = ids[start : start + _IN_BATCH]
        rows = await db.execute(
            select(
                PlanDecision.id,
                PlanDecision.job_id,
                PlanDecision.attempt_no,
                PlanDecision.replay,
            ).where(PlanDecision.id.in_(batch))
        )
        for decision_id, job_id, attempt_no, replay in rows:
            out[int(decision_id)] = _Decision(str(job_id), int(attempt_no), replay)
    return out


async def _split_parts(
    db: AsyncSession, jobs: Sequence[str]
) -> tuple[
    dict[str, list[int]],
    dict[str, list[tuple[int, LaneOutcome]]],
    dict[str, CombineOutcome],
]:
    """(job -> its dispatched decisions' attempt_no, sorted; job -> (token,
    lane) list; job -> done combine)."""
    dispatched: dict[str, list[int]] = {}
    lanes: dict[str, list[tuple[int, Attempt]]] = {}
    combines: dict[str, Attempt] = {}
    for batch in _batches(sorted(set(jobs))):
        for job_id, attempt_no in await db.execute(
            select(PlanDecision.job_id, PlanDecision.attempt_no).where(
                PlanDecision.job_id.in_(batch), PlanDecision.outcome == "dispatched"
            )
        ):
            dispatched.setdefault(str(job_id), []).append(int(attempt_no))
        for attempt in await db.scalars(
            select(Attempt).where(
                Attempt.job_id.in_(batch), Attempt.role.in_(("lane", "combine"))
            )
        ):
            if attempt.role == "lane":
                lanes.setdefault(attempt.job_id, []).append(
                    (int(attempt.fencing_token), attempt)
                )
            elif attempt.state == "done" and attempt.ended_at is not None:
                held = combines.get(attempt.job_id)
                if held is None or (attempt.ended_at, attempt.id) > (
                    held.ended_at,  # never None: only ended attempts are held
                    held.id,
                ):
                    combines[attempt.job_id] = attempt
    ids = [a.id for items in lanes.values() for _, a in items] + [
        a.id for a in combines.values()
    ]
    phases = await _phases(db, ids)
    chunks: dict[str, list[ChunkOutcome]] = {}
    lane_ids = sorted(a.id for items in lanes.values() for _, a in items)
    for batch in _batches(lane_ids):
        for lane_id, items, run_ms, outcome in await db.execute(
            select(
                ChunkRecord.lane_attempt_id,
                ChunkRecord.items,
                ChunkRecord.run_ms,
                ChunkRecord.outcome,
            ).where(ChunkRecord.lane_attempt_id.in_(batch))
        ):
            chunks.setdefault(str(lane_id), []).append(
                ChunkOutcome(
                    items=int(items),
                    run_ms=None if run_ms is None else int(run_ms),
                    outcome=str(outcome),
                )
            )
    lane_out = {
        job_id: [
            (
                token,
                LaneOutcome(
                    attempt_id=a.id,
                    node_id=a.node_id,
                    state=a.state,
                    phases_ms=phases.get(a.id, {}),
                    finished_at=a.ended_at,
                    chunks=tuple(chunks.get(a.id, ())),
                ),
            )
            for token, a in sorted(items, key=lambda item: (item[0], item[1].id))
        ]
        for job_id, items in lanes.items()
    }
    combine_out = {
        job_id: CombineOutcome(
            attempt_id=a.id,
            node_id=a.node_id,
            state=a.state,
            phases_ms=phases.get(a.id, {}),
            finished_at=a.ended_at,
        )
        for job_id, a in combines.items()
    }
    return {k: sorted(v) for k, v in dispatched.items()}, lane_out, combine_out


async def _page(db: AsyncSession, outcomes: list[DecisionOutcome]) -> list[RunInput]:
    decisions = await _decisions(db, [o.decision_id for o in outcomes])
    split_jobs = [
        decisions[o.decision_id].job_id
        for o in outcomes
        if o.decision_id in decisions and _is_split(o)
    ]
    dispatched, lanes, combines = (
        await _split_parts(db, split_jobs) if split_jobs else ({}, {}, {})
    )
    runs: list[RunInput] = []
    for outcome in outcomes:
        decision = decisions.get(outcome.decision_id)
        if decision is None:  # deleted between the two reads
            continue
        job_lanes: tuple[LaneOutcome, ...] = ()
        combine: CombineOutcome | None = None
        if _is_split(outcome):
            # A round's lanes: fencing tokens in [attempt_no, next dispatched
            # attempt_no). Relies on split/lanes.create_lanes giving a round's
            # lanes consecutive tokens from the decision's attempt_no in the
            # dispatch transaction; a later round always starts above them.
            tokens = dispatched.get(decision.job_id, [])
            later = [t for t in tokens if t > decision.attempt_no]
            end = min(later) if later else None
            job_lanes = tuple(
                lane
                for token, lane in lanes.get(decision.job_id, [])
                if token >= decision.attempt_no and (end is None or token < end)
            )
            if end is None:
                combine = combines.get(decision.job_id)
        runs.append(
            RunInput(
                outcome=outcome,
                job_id=decision.job_id,
                replay=decision.replay,
                lanes=job_lanes,
                combine=combine,
            )
        )
    return runs


async def iter_runs(
    engine: AsyncEngine,
    *,
    max_runs: int = DEFAULT_MAX_RUNS,
    page_size: int = PAGE_SIZE,
) -> AsyncIterator[Page]:
    """The runs, newest first, a page at a time (module docstring);
    :class:`MlLoadError` when the database fails."""
    sessions = create_session_factory(engine)
    cursor: tuple[dt.datetime, int] | None = None
    read = 0
    limit = max(1, max_runs)
    try:
        while read < limit:
            want = min(max(1, page_size), limit - read)
            async with sessions() as db:
                outcomes = await read_outcomes(db, want, before=cursor)
                if not outcomes:
                    return
                runs = await _page(db, outcomes)
                read += len(outcomes)
                last = outcomes[-1]
                cursor = (last.finished_at, last.decision_id)
                truncated = False
                if read >= limit and len(outcomes) == want:
                    truncated = bool(await read_outcomes(db, 1, before=cursor))
            yield Page(runs=runs, truncated=truncated)
            if len(outcomes) < want:
                return
    except _DB_ERRORS as exc:
        raise MlLoadError(
            "the ML dataset read failed in the database",
            details={"cause": type(exc).__name__},
        ) from exc


# --- V0.15 G2 (D354, D355) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Watermark:
    """What a built dataset depends on, cheaply (D355): equal watermarks ->
    the cached build is still the dataset."""

    matched_runs: int
    newest_matched: tuple[dt.datetime, int] | None
    """The newest matched run's ``(ended_at, decision id)``."""
    ended_attempts: int
    """Every ended attempt (any role, any state): a lane, a combine or a
    failure that ends later changes a Mode 3 row or ``node_failures``
    without touching the matched set."""
    newest_end: dt.datetime | None
    feature_version: int
    max_runs: int
    run_records: int = 0
    """V0.15 G3 (G2 tech debt): run records written. A run record can land
    after its attempt ended (the recorder is asynchronous) and changes the
    row's family / fingerprint - a "relabel" the attempt counts above do not
    see. Run records are insert-only (``ON CONFLICT DO NOTHING``), so their
    count and newest ``recorded_at`` cover every change."""
    newest_record: dt.datetime | None = None

    def as_json(self) -> dict[str, Any]:
        """A JSON object of every field (V0.15 G3: stored with a model set,
        so the retrain task can skip unchanged data across restarts)."""

        def iso(value: dt.datetime | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "matched_runs": self.matched_runs,
            "newest_matched": None
            if self.newest_matched is None
            else [iso(self.newest_matched[0]), self.newest_matched[1]],
            "ended_attempts": self.ended_attempts,
            "newest_end": iso(self.newest_end),
            "feature_version": self.feature_version,
            "max_runs": self.max_runs,
            "run_records": self.run_records,
            "newest_record": iso(self.newest_record),
        }


async def read_watermark(engine: AsyncEngine, *, max_runs: int) -> Watermark:
    """The current :class:`Watermark` (four aggregates, no row read);
    :class:`MlLoadError` when the database fails."""
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            count, newest_at, newest_id = await matched_watermark(db)
            ended, newest_end = (
                await db.execute(
                    select(func.count(), func.max(Attempt.ended_at)).where(
                        Attempt.ended_at.is_not(None)
                    )
                )
            ).one()
            records, newest_record = (
                await db.execute(
                    select(func.count(), func.max(RunRecord.recorded_at)).select_from(
                        RunRecord
                    )
                )
            ).one()
    except _DB_ERRORS as exc:
        raise MlLoadError(
            "the ML dataset watermark read failed in the database",
            details={"cause": type(exc).__name__},
        ) from exc
    return Watermark(
        matched_runs=count,
        newest_matched=None
        if newest_at is None or newest_id is None
        else (newest_at, newest_id),
        ended_attempts=int(ended or 0),
        newest_end=newest_end,
        feature_version=FEATURE_VERSION,
        max_runs=max_runs,
        run_records=int(records or 0),
        newest_record=newest_record,
    )


async def count_node_failures(engine: AsyncEngine, *, since: dt.datetime | None) -> int:
    """D354: attempts that ended node-caused (the D296 risk term's
    :func:`~meshforge.controller.jobs.queue_reads.node_caused_end`, never a
    second definition) since ``since`` (None = ever) - the events a failure
    classifier would learn from. :class:`MlLoadError` when the database
    fails."""
    query = (
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.ended_at.is_not(None), node_caused_end())
    )
    if since is not None:
        query = query.where(Attempt.ended_at >= since)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            return int(await db.scalar(query) or 0)
    except _DB_ERRORS as exc:
        raise MlLoadError(
            "the ML dataset failure count failed in the database",
            details={"cause": type(exc).__name__},
        ) from exc
