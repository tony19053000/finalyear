"""Stored evaluation reports in PostgreSQL (V0.14 G4, D318; table ``planner_eval_report``).

:class:`EvalReportStore` is the table's one writer and its reads:

* :meth:`~EvalReportStore.insert` - one report as a new row (``campaign_ids``,
  ``holdout`` and ``leaked`` copied from the report, so the columns can never
  disagree with it);
* :meth:`~EvalReportStore.recent` - the newest reports as summaries (no
  ``groups``: the database projects ``verdicts``, ``eval_version`` and the
  evaluated-group count out of the JSON, so a list never ships megabytes);
* :meth:`~EvalReportStore.get` - one full report.

A database that refuses or drops the call raises :class:`EvalStoreError`
(retryable, the API answers 503).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.planner_eval import PlannerEvalReport

__all__ = [
    "MAX_LIST",
    "EvalReportRow",
    "EvalReportStore",
    "EvalReportSummary",
    "EvalStoreError",
]

MAX_LIST: Final = 200
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)


class EvalStoreError(MeshForgeError):
    """The database refused or lost an evaluation-report read/write."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class EvalReportRow:
    id: int
    created_at: dt.datetime
    campaign_ids: list[str]
    holdout: str | None
    leaked: bool
    report: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EvalReportSummary:
    id: int
    created_at: dt.datetime
    campaign_ids: list[str]
    holdout: str | None
    leaked: bool
    eval_version: int
    evaluated_groups: int
    verdicts: list[dict[str, Any]]


def _row(item: PlannerEvalReport) -> EvalReportRow:
    return EvalReportRow(
        id=int(item.id),
        created_at=item.created_at,
        campaign_ids=list(item.campaign_ids),
        holdout=item.holdout,
        leaked=bool(item.leaked),
        report=dict(item.report),
    )


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class EvalReportStore:
    """See module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    @staticmethod
    def _error(exc: BaseException, what: str) -> EvalStoreError:
        return EvalStoreError(
            f"the evaluation report {what} failed in the database",
            details={"cause": type(exc).__name__},
        )

    async def insert(self, report: Mapping[str, Any]) -> EvalReportRow:
        item = PlannerEvalReport(
            campaign_ids=list(report["campaign_ids"]),
            holdout=report["holdout"],
            leaked=bool(report["leaked"]),
            report=dict(report),
        )
        try:
            async with self._sessions() as db, db.begin():
                db.add(item)
                await db.flush()
                await db.refresh(item)
                return _row(item)
        except _DB_ERRORS as exc:
            raise self._error(exc, "write") from exc

    async def get(self, report_id: int) -> EvalReportRow | None:
        try:
            async with self._sessions() as db:
                found = await db.get(PlannerEvalReport, report_id)
                return None if found is None else _row(found)
        except _DB_ERRORS as exc:
            raise self._error(exc, "read") from exc

    async def recent(self, limit: int = 50) -> list[EvalReportSummary]:
        report = PlannerEvalReport.report
        query = (
            select(
                PlannerEvalReport.id,
                PlannerEvalReport.created_at,
                PlannerEvalReport.campaign_ids,
                PlannerEvalReport.holdout,
                PlannerEvalReport.leaked,
                report["eval_version"],
                report["counts"]["evaluated_groups"],
                report["verdicts"],
            )
            .order_by(PlannerEvalReport.id.desc())
            .limit(max(1, min(limit, MAX_LIST)))
        )
        try:
            async with self._sessions() as db:
                rows = list((await db.execute(query)).all())
        except _DB_ERRORS as exc:
            raise self._error(exc, "read") from exc
        return [
            EvalReportSummary(
                id=int(r[0]),
                created_at=r[1],
                campaign_ids=list(r[2]),
                holdout=r[3],
                leaked=bool(r[4]),
                eval_version=_int(r[5]),
                evaluated_groups=_int(r[6]),
                verdicts=[v for v in (r[7] or []) if isinstance(v, dict)],
            )
            for r in rows
        ]
