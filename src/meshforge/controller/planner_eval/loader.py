"""The evaluator's database reads (V0.14 G4, D297/D313; ADR-0018 data).

No new pipeline (D297): the evaluator reads exactly what the labels read,
plus the decisions' replays.

* **Groups** - :func:`~meshforge.controller.telemetry.labels.build_labels`
  over the campaign's planned runs and its label rows (the one row filter,
  :func:`~meshforge.controller.telemetry.labels_store.label_rows`), with each
  configuration's ``node_set`` from ``bench_run`` (the labels do not carry it;
  Mode 3 mapping needs it, D314).
* **Records** - the same label rows (latest attempt per configuration and
  repetition), as :class:`~.model.EvalRecord`. They are column -> value
  mappings exactly as the Parquet export holds them
  (:func:`records_from_rows` takes either).
* **Latency** (V0.14 G6, D324) - every ``dispatched`` decision of the
  campaigns' benchmark jobs with its ``decision_ms``, snapshot size and
  live snapshot nodes (D332) (:func:`load_latency`).
* **Replays** - per record the ``dispatched`` ``plan_decision`` of its attempt
  (a whole job), or else its job's **first** ``dispatched`` decision (a split
  job's record is the combine's row; the lanes were planned by that first
  decision, D259). Read in batches; ``replay`` NULL stays None (counted by
  the core).

:func:`load_for_jobs` is D319's read: the labelled groups of the campaigns
that hold the given jobs, restricted to the groups those jobs ran in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from sqlalchemy import case, cast, column, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.controller.bench.control import campaign_jobs
from meshforge.controller.planner_eval.latency import (
    LIVE_CAPABILITY,
    LIVE_STATE,
    LatencySample,
)
from meshforge.controller.planner_eval.model import ConfigRef, EvalGroup, EvalRecord
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.bench import BenchCampaign, BenchRun
from meshforge.controller.store.models.job import Job
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.labels import CampaignLabels, build_labels
from meshforge.controller.telemetry.labels_store import (
    LabelRowsLimitError,
    label_rows,
    planned_runs,
)
from meshforge.controller.telemetry.recorder import TelemetryStoreError

__all__ = [
    "MAX_D319_CAMPAIGNS",
    "EvalInputs",
    "EvalLoadError",
    "EvalTooLargeError",
    "attach_replays",
    "groups_from_labels",
    "latency_samples",
    "load_campaigns",
    "load_for_jobs",
    "load_latency",
    "missing_campaigns",
    "records_from_rows",
]

_BATCH: Final = 500
MAX_D319_CAMPAIGNS: Final = 20
"""Most campaigns one D319 regret check reads (the newest by id)."""
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)


class EvalLoadError(MeshForgeError):
    """The database refused or lost an evaluator read (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


class EvalTooLargeError(MeshForgeError):
    """A campaign has more label rows than the cap (not retryable)."""

    default_failure_type = FailureType.INVALID_INPUT


@dataclass(frozen=True, slots=True)
class EvalInputs:
    groups: tuple[EvalGroup, ...]
    records: tuple[EvalRecord, ...]
    latency: tuple[LatencySample, ...] = ()
    """V0.14 G6 (D324): the campaigns' dispatched decisions' times."""


def _error(exc: BaseException) -> EvalLoadError:
    return EvalLoadError(
        "the evaluator's read failed in the database",
        details={"cause": type(exc).__name__},
    )


def groups_from_labels(
    labels: CampaignLabels, node_sets: Mapping[str, Sequence[str]]
) -> list[EvalGroup]:
    """The campaign's labels as :class:`~.model.EvalGroup` s (pure)."""
    out: list[EvalGroup] = []
    for group in labels.groups:
        configs = tuple(
            ConfigRef(
                config_id=c.config_id,
                strategy=c.strategy,
                node_id=c.node_id,
                origin_node_id=c.origin_node_id,
                node_set=tuple(sorted(node_sets.get(c.config_id) or ())),
                state=c.state,
                median_ms=None
                if (s := c.metrics.get("total_ms")) is None
                else float(s.median),
                repetitions=c.repetitions,
            )
            for c in group.configs
        )
        out.append(
            EvalGroup(
                campaign_id=labels.campaign_id,
                group_id=group.group_id,
                family=group.family,
                condition=group.condition,
                label=group.label,
                best_config_id=group.best_config_id,
                tied_config_ids=tuple(group.tied_config_ids),
                configs=configs,
                reason=group.reason,
            )
        )
    return out


def records_from_rows(
    campaign_id: str, rows: Iterable[Mapping[str, Any]]
) -> list[EvalRecord]:
    """Label rows (database or Parquet export: the same column names) as
    :class:`~.model.EvalRecord` s without replays (pure)."""
    out: list[EvalRecord] = []
    for row in rows:
        config, group, rep = (
            row.get("config_id"),
            row.get("group_id"),
            row.get("repetition"),
        )
        if not isinstance(config, str) or not isinstance(group, str):
            continue
        if isinstance(rep, bool) or not isinstance(rep, int):
            continue
        fingerprint = row.get("fingerprint")
        out.append(
            EvalRecord(
                campaign_id=campaign_id,
                group_id=group,
                config_id=config,
                repetition=rep,
                attempt_id=str(row.get("attempt_id")),
                job_id=str(row.get("job_id")),
                fingerprint=fingerprint if isinstance(fingerprint, str) else None,
            )
        )
    return out


async def missing_campaigns(engine: AsyncEngine, ids: Sequence[str]) -> list[str]:
    """The ids of ``ids`` that name no campaign."""
    wanted = sorted(set(ids))
    try:
        async with create_session_factory(engine)() as db:
            found = set(
                await db.scalars(
                    select(BenchCampaign.id).where(BenchCampaign.id.in_(wanted))
                )
            )
    except _DB_ERRORS as exc:
        raise _error(exc) from exc
    return [i for i in wanted if i not in found]


async def _node_sets(engine: AsyncEngine, campaign_id: str) -> dict[str, list[str]]:
    query = (
        select(BenchRun.config_id, BenchRun.node_set)
        .where(BenchRun.campaign_id == campaign_id, BenchRun.node_set.is_not(None))
        .distinct()
    )
    async with create_session_factory(engine)() as db:
        rows = list((await db.execute(query)).all())
    return {str(r[0]): [str(n) for n in (r[1] or [])] for r in rows}


def attach_replays(
    records: Sequence[EvalRecord], rows: Iterable[Sequence[Any]]
) -> list[EvalRecord]:
    """``records`` with their decisions' replays (pure; module docstring).
    ``rows`` = ``(decision id, job id, attempt id, replay)`` of ``dispatched``
    decisions, **ascending by decision id**: a record takes its own
    attempt's decision, else its job's **first** one (a split job's record is
    the combine's row; the lanes were planned by the first decision - a later
    one is a re-plan of what was left, D259)."""
    by_attempt: dict[str, tuple[int, Any]] = {}
    first_of_job: dict[str, tuple[int, Any]] = {}
    for decision_id, job_id, attempt_id, replay in rows:
        found = (int(decision_id), replay)
        if attempt_id is not None:
            by_attempt.setdefault(str(attempt_id), found)
        first_of_job.setdefault(str(job_id), found)
    out: list[EvalRecord] = []
    for record in records:
        hit = by_attempt.get(record.attempt_id) or first_of_job.get(record.job_id)
        if hit is None:
            out.append(record)
        else:
            out.append(replace(record, decision_id=hit[0], replay=hit[1]))
    return out


async def _replays(
    engine: AsyncEngine, records: Sequence[EvalRecord]
) -> list[EvalRecord]:
    """``records`` with their decisions' replays, read in batches of jobs
    (:func:`attach_replays`)."""
    jobs = sorted({r.job_id for r in records})
    rows: list[Sequence[Any]] = []
    sessions = create_session_factory(engine)
    for start in range(0, len(jobs), _BATCH):
        batch = jobs[start : start + _BATCH]
        query = (
            select(
                PlanDecision.id,
                PlanDecision.job_id,
                PlanDecision.attempt_id,
                PlanDecision.replay,
            )
            .where(PlanDecision.outcome == "dispatched", PlanDecision.job_id.in_(batch))
            .order_by(PlanDecision.id)
        )
        async with sessions() as db:
            rows.extend((await db.execute(query)).all())
    rows.sort(key=lambda r: int(r[0]))  # one order over every batch
    return attach_replays(records, rows)


def _node_count(nodes: Any, candidates: Any) -> int | None:
    """A decision's snapshot size: the replay's node count, else the distinct
    candidate nodes (a row without a replay); None = unreadable."""
    if isinstance(nodes, int) and not isinstance(nodes, bool) and nodes >= 0:
        return nodes
    if not isinstance(candidates, list):
        return None
    seen: set[str] = set()
    for row in candidates:
        if not isinstance(row, Mapping):
            continue
        node = row.get("node_id")
        if isinstance(node, str):
            seen.add(node)
        members = row.get("node_set")
        if isinstance(members, list):
            seen.update(m for m in members if isinstance(m, str))
    return len(seen) if seen else None


def _count(value: Any) -> int | None:
    """A non-negative integer count, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def latency_samples(rows: Iterable[Sequence[Any]]) -> list[LatencySample]:
    """``(decision_ms, replay node count, replay live node count,
    candidates)`` rows as :class:`~.latency.LatencySample` s (pure). The live
    count has no fallback: a row without a replay is never judged (D332)."""
    out: list[LatencySample] = []
    for ms, nodes, live, candidates in rows:
        value = (
            float(ms)
            if isinstance(ms, int | float) and not isinstance(ms, bool)
            else None
        )
        out.append(LatencySample(value, _node_count(nodes, candidates), _count(live)))
    return out


async def load_latency(
    engine: AsyncEngine, campaign_ids: Sequence[str]
) -> list[LatencySample]:
    """V0.14 G6 (D324): every ``dispatched`` decision of the campaigns'
    benchmark jobs (:func:`~meshforge.controller.bench.control.campaign_jobs`:
    runs of every generation, fillers, warm-ups) with its ``decision_ms`` and
    snapshot size - the replay's node count read in SQL (the 64 KiB blob is
    never loaded), the candidates only for a row without a replay - and
    (D332) the replay's live nodes, also counted in SQL: a correlated count
    over the snapshot's node rows that are ``online``, ``connected`` and
    accept jobs (:data:`~.latency.LIVE_STATE`,
    :data:`~.latency.LIVE_CAPABILITY`). Ordered by decision id."""
    nodes = PlanDecision.replay["snapshot"]["nodes"]
    is_array = func.jsonb_typeof(nodes) == "array"
    rows_of = func.jsonb_array_elements(
        case((is_array, nodes), else_=cast(literal("[]"), JSONB))
    ).table_valued(column("value", JSONB))
    node = rows_of.c.value
    live = (
        select(func.count())
        .select_from(rows_of)
        .where(
            node["state"].astext == LIVE_STATE,
            node["connected"] == cast(literal("true"), JSONB),
            node["capabilities"].contains([LIVE_CAPABILITY]),
        )
        .scalar_subquery()
    )
    query = (
        select(
            PlanDecision.decision_ms,
            case((is_array, func.jsonb_array_length(nodes)), else_=None),
            case((is_array, live), else_=None),
            case((PlanDecision.replay.is_(None), PlanDecision.candidates), else_=None),
        )
        .join(Job, Job.id == PlanDecision.job_id)
        .where(PlanDecision.outcome == "dispatched", campaign_jobs(campaign_ids))
        .order_by(PlanDecision.id)
    )
    async with create_session_factory(engine)() as db:
        rows = list((await db.execute(query)).all())
    return latency_samples(rows)


async def load_campaigns(
    engine: AsyncEngine, campaign_ids: Sequence[str], *, max_rows: int
) -> EvalInputs:
    """Every group and record of ``campaign_ids`` (module docstring).
    :class:`EvalTooLargeError` when a campaign has more than ``max_rows``
    label rows; :class:`EvalLoadError` when the database fails."""
    groups: list[EvalGroup] = []
    records: list[EvalRecord] = []
    try:
        for campaign_id in sorted(set(campaign_ids)):
            runs = await planned_runs(engine, campaign_id)
            rows = await label_rows(engine, campaign_id, limit=max_rows)
            labels = build_labels(campaign_id, runs, rows)
            groups.extend(
                groups_from_labels(labels, await _node_sets(engine, campaign_id))
            )
            records.extend(records_from_rows(campaign_id, rows))
        with_replays = await _replays(engine, records)
        latency = await load_latency(engine, campaign_ids)
    except LabelRowsLimitError as exc:
        raise EvalTooLargeError(
            f"a campaign has more than {max_rows} records; evaluate fewer "
            "campaigns or raise [controller.telemetry] export_max_rows",
            details={"limit": max_rows},
        ) from exc
    except TelemetryStoreError as exc:
        raise _error(exc) from exc
    except _DB_ERRORS as exc:
        raise _error(exc) from exc
    return EvalInputs(
        groups=tuple(groups), records=tuple(with_replays), latency=tuple(latency)
    )


async def load_for_jobs(
    engine: AsyncEngine, job_ids: Iterable[str], *, max_rows: int
) -> EvalInputs:
    """D319: the groups (and their records) of the campaigns holding
    ``job_ids`` that one of those jobs ran in (newest
    :data:`MAX_D319_CAMPAIGNS` campaigns)."""
    jobs = sorted(set(job_ids))
    if not jobs:
        return EvalInputs((), ())
    campaigns: set[str] = set()
    try:
        async with create_session_factory(engine)() as db:
            for start in range(0, len(jobs), _BATCH):
                found = await db.scalars(
                    select(RunRecord.campaign_id)
                    .where(
                        RunRecord.job_id.in_(jobs[start : start + _BATCH]),
                        RunRecord.campaign_id.is_not(None),
                    )
                    .distinct()
                )
                campaigns.update(str(c) for c in found)
    except _DB_ERRORS as exc:
        raise _error(exc) from exc
    chosen = sorted(campaigns, reverse=True)[:MAX_D319_CAMPAIGNS]
    loaded = await load_campaigns(engine, chosen, max_rows=max_rows)
    wanted = set(jobs)
    keys = {(r.campaign_id, r.group_id) for r in loaded.records if r.job_id in wanted}
    return EvalInputs(
        groups=tuple(g for g in loaded.groups if (g.campaign_id, g.group_id) in keys),
        records=tuple(r for r in loaded.records if (r.campaign_id, r.group_id) in keys),
    )
