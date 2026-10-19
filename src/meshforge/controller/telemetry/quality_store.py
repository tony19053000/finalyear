"""The data quality report's reads (V0.9 G5c, roadmap 0.9.8, D220/D254).

:func:`campaign_quality` streams **every** run record of a campaign (the
report must see failures, unlike the labels) through the pure
:class:`meshforge.bench.quality.QualityInput`, reads the planned runs with
the labels' :func:`.labels_store.planned_runs` and the failure counts with
:func:`meshforge.controller.bench.control.record_failure_counts` (the same
query as ``compute bench status``), then builds the report.

**One row filter.** Each record carries ``label_row`` = the labels' own
WHERE clauses (:func:`.labels_store.label_filter`) evaluated as a column -
the report never re-implements "which records count", so its medians are the
labels' medians. ``bench_role`` (fillers / warm-ups) is selected separately
and counted, never measured.

**Bounded memory.** Only the columns the report needs
(:data:`meshforge.bench.quality.ROW_FIELDS`) are selected, streamed in
batches of :data:`STREAM_BATCH` rows (``yield_per``); the accumulator keeps the
ok rows' few fields and counters, never ORM objects.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Final

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.bench.quality import (
    DEFAULT_CV_THRESHOLD,
    ROW_FIELDS,
    CampaignQuality,
    QualityInput,
    build_quality,
)
from meshforge.controller.bench.control import record_failure_counts
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.labels_store import label_filter, planned_runs
from meshforge.controller.telemetry.recorder import TelemetryStoreError

__all__ = ["STREAM_BATCH", "campaign_quality"]

STREAM_BATCH: Final = 1_000


def _store_error(exc: SQLAlchemyError) -> TelemetryStoreError:
    return TelemetryStoreError(
        "the database refused the quality query",
        details={"cause": type(exc).__name__},
    )


async def campaign_quality(
    engine: AsyncEngine,
    campaign_id: str,
    *,
    campaign_state: str,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
) -> CampaignQuality:
    """The campaign's quality report (module docstring). Raises
    :class:`~.recorder.TelemetryStoreError`."""
    runs = await planned_runs(engine, campaign_id)
    columns = [getattr(RunRecord, name) for name in ROW_FIELDS]
    query = (
        select(
            *columns,
            and_(*label_filter(campaign_id)).label("label_row"),
            RunRecord.condition_measured["bench_role"].astext.label("bench_role"),
            RunRecord.condition_measured["verified"].astext.label("condition_verified"),
        )
        .where(RunRecord.campaign_id == campaign_id)
        .order_by(RunRecord.attempt_id)
        .execution_options(yield_per=STREAM_BATCH)
    )
    data = QualityInput()
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            # one snapshot: the failure counts and the streamed records agree
            await db.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            failures = await record_failure_counts(db, campaign_id)
            result = await db.stream(query)
            async for row in result.mappings():
                data.add(dict(row))
    except SQLAlchemyError as exc:
        raise _store_error(exc) from exc
    # the statistics run in a worker thread (sorting 10 000 values must not
    # stall the event loop); the inputs are this call's own, shared with no one
    return await asyncio.to_thread(
        functools.partial(
            build_quality,
            campaign_id,
            campaign_state=campaign_state,
            runs=runs,
            data=data,
            failures=failures,
            cv_threshold=cv_threshold,
        )
    )
