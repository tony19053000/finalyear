"""The label row filter and the campaign reads behind labels (V0.9 G5, 0.9.6, D218/D244/D246).

:func:`label_rows` is **the** row filter for anything that learns from a
campaign's records - the labels now, planner training (V0.15) later. A record
counts only if all of these hold (:func:`label_filter`):

* ``campaign_id`` = the campaign;
* ``failure_class = 'ok'`` - the attempt finished; a ``workload`` failure, an
  ``infrastructure`` failure, a ``condition_failed`` run (the condition could
  not be applied or verified) and an ``unavailable`` run (no CUDA, cancelled)
  are not measurements of the configuration;
* ``workload_status = 'ok'`` - the benchmark's own self-check passed (a wrong
  answer computed fast is not fast);
* ``condition_measured->>'bench_role' IS NULL`` - **D244**: a campaign's queue
  fillers and cache warm-ups get ordinary records tagged ``bench_role``; they
  are scaffolding for another run's condition and must never count, even if
  a later change gives them the campaign's identity;
* ``condition_measured->>'verified'`` is not ``'false'`` - belt and braces: an
  ``ok`` record whose condition was not verified would otherwise be labelled
  with a condition it did not run under.

Then, per (config, repetition), only the **latest** attempt is kept
(:func:`.labels.latest_per_repetition`).

:func:`planned_runs` reads the campaign's ``bench_run`` rows with, per run,
whether its job's record exists (an ``ok`` one for a ``done`` run, any for a
``failed`` one) - the ``pending`` rule of :mod:`.labels`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.bench import BenchRun
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.labels import (
    PlannedRunState,
    latest_per_repetition,
)
from meshforge.controller.telemetry.recorder import TelemetryStoreError
from meshforge.controller.telemetry.schema import COLUMN_NAMES, FailureClass

__all__ = [
    "LabelRowsLimitError",
    "label_filter",
    "label_rows",
    "planned_runs",
]

_OK: Final = FailureClass.OK.value


class LabelRowsLimitError(MeshForgeError):
    """More matching records than the caller's cap (the export's row cap) -
    not retryable: the answer would be the same."""

    default_failure_type = FailureType.INVALID_INPUT


def label_filter(campaign_id: str) -> list[ColumnElement[bool]]:
    """The WHERE clauses of :func:`label_rows` (module docstring) - one list,
    so labels and training cannot drift apart."""
    measured = RunRecord.condition_measured
    return [
        RunRecord.campaign_id == campaign_id,
        RunRecord.failure_class == _OK,
        RunRecord.workload_status == "ok",
        # D244: fillers / warm-ups never count
        measured["bench_role"].astext.is_(None),
        or_(
            measured["verified"].astext.is_(None),
            measured["verified"].astext != "false",
        ),
    ]


def _store_error(exc: SQLAlchemyError) -> TelemetryStoreError:
    return TelemetryStoreError(
        "the database refused the label query",
        details={"cause": type(exc).__name__},
    )


async def label_rows(
    engine: AsyncEngine, campaign_id: str, *, limit: int
) -> list[Mapping[str, Any]]:
    """The campaign's label rows (module docstring) as column -> value
    mappings, one per (config, repetition), sorted by (group_id, config_id,
    repetition). More than ``limit`` matching records -> :class:`LabelRowsLimitError`.
    Raises :class:`~.recorder.TelemetryStoreError`."""
    query = (
        select(RunRecord)
        .where(*label_filter(campaign_id))
        .order_by(RunRecord.config_id, RunRecord.repetition, RunRecord.attempt_id)
        .limit(limit + 1)
    )
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            found: Sequence[RunRecord] = list(await db.scalars(query))
    except SQLAlchemyError as exc:
        raise _store_error(exc) from exc
    if len(found) > limit:
        raise LabelRowsLimitError(
            f"more than {limit} records match", details={"limit": limit}
        )
    rows = [{name: getattr(row, name) for name in COLUMN_NAMES} for row in found]
    return latest_per_repetition(rows)


async def planned_runs(engine: AsyncEngine, campaign_id: str) -> list[PlannedRunState]:
    """The campaign's runs (``seq`` order) with their ``recorded`` flag.

    The records are matched by **job id** (not by the record's campaign id,
    so a record written without the campaign identity still counts), in one
    hash join over the campaign's runs - ``run_record`` has no ``job_id``
    index and a per-run lookup would scan it once per run."""
    per_job = (
        select(
            RunRecord.job_id.label("job_id"),
            func.bool_or(RunRecord.failure_class == _OK).label("any_ok"),
        )
        .join(BenchRun, BenchRun.job_id == RunRecord.job_id)
        .where(BenchRun.campaign_id == campaign_id)
        .group_by(RunRecord.job_id)
        .subquery()
    )
    query = (
        select(
            BenchRun.group_id,
            BenchRun.config_id,
            BenchRun.repetition,
            BenchRun.family,
            BenchRun.params,
            BenchRun.conditions,
            BenchRun.strategy,
            BenchRun.node_id,
            BenchRun.origin_node_id,
            BenchRun.state,
            BenchRun.detail,
            per_job.c.job_id.is_not(None).label("has_record"),
            per_job.c.any_ok,
        )
        .outerjoin(per_job, per_job.c.job_id == BenchRun.job_id)
        .where(BenchRun.campaign_id == campaign_id)
        .order_by(BenchRun.seq)
    )
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            result = await db.execute(query)
            raw = list(result.all())
    except SQLAlchemyError as exc:
        raise _store_error(exc) from exc
    return [
        PlannedRunState(
            group_id=r.group_id,
            config_id=r.config_id,
            repetition=int(r.repetition),
            family=r.family,
            params=dict(r.params or {}),
            condition=str((r.conditions or ["none"])[0]),
            strategy=r.strategy,
            node_id=r.node_id,
            origin_node_id=r.origin_node_id,
            state=r.state,
            detail=r.detail,
            # a done run needs its ok record; a failed run any record
            recorded=bool(r.has_record) and (r.state != "done" or bool(r.any_ok)),
        )
        for r in raw
    ]
