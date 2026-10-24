"""Stored evaluation reports in PostgreSQL (V0.14 G4, D318; table ``planner_eval_report``).

:class:`EvalReportStore` is the table's one writer and its reads:

* :meth:`~EvalReportStore.insert` - one report as a new row (``campaign_ids``,
  ``holdout`` and ``leaked`` copied from the report, so the columns can never
  disagree with it);
* (V0.15 G6b, D378 - start-then-poll, no migration) :meth:`~EvalReportStore.
  start` - a **running** row: its ``report`` is only ``{"run_state":
  {"status": "running"}}`` until the evaluation ends;
  :meth:`~EvalReportStore.finish` replaces it with the report (status
  ``done``), :meth:`~EvalReportStore.fail` with ``{"run_state": {"status":
  "failed", "reason", "detail"}}``; both change only a row still running.
  :meth:`~EvalReportStore.sweep_running` fails every running row (the
  controller's start-up: an evaluation never outlives its process, reason
  ``controller_restart``). A stored report never has a ``run_state`` key, so
  a row without one is ``done`` (every row written before G6b too);
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

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.planner_eval import PlannerEvalReport

__all__ = [
    "CONTROLLER_RESTART",
    "MAX_LIST",
    "RUN_STATE_KEY",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "EvalReportRow",
    "EvalReportStore",
    "EvalReportSummary",
    "EvalStoreError",
]

MAX_LIST: Final = 200
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)

RUN_STATE_KEY: Final = "run_state"
"""The key of a not-done row's ``report`` (module docstring)."""
STATUS_RUNNING: Final = "running"
STATUS_DONE: Final = "done"
STATUS_FAILED: Final = "failed"
CONTROLLER_RESTART: Final = "controller_restart"
"""``reason`` of an evaluation that was running when its controller
stopped (the start-up sweep, D378)."""
_MAX_DETAIL: Final = 500


def run_state(report: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """``(status, reason, detail)`` of a row's ``report`` (module
    docstring): no ``run_state`` key = ``done``."""
    state = report.get(RUN_STATE_KEY)
    if not isinstance(state, Mapping):
        return STATUS_DONE, None, None
    status = state.get("status")
    reason = state.get("reason")
    detail = state.get("detail")
    return (
        STATUS_RUNNING if status == STATUS_RUNNING else STATUS_FAILED,
        reason if isinstance(reason, str) else None,
        detail if isinstance(detail, str) else None,
    )


def failed_state(reason: str, detail: str | None) -> dict[str, Any]:
    """The ``report`` of a failed row."""
    return {
        RUN_STATE_KEY: {
            "status": STATUS_FAILED,
            "reason": reason,
            "detail": None if detail is None else detail[:_MAX_DETAIL],
        }
    }


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
    """The report (``status`` ``done``), else the ``run_state`` document."""

    @property
    def status(self) -> str:
        return run_state(self.report)[0]

    @property
    def reason(self) -> str | None:
        return run_state(self.report)[1]

    @property
    def detail(self) -> str | None:
        return run_state(self.report)[2]


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
    status: str = STATUS_DONE
    reason: str | None = None


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

    async def start(
        self, campaign_ids: list[str], *, holdout: str | None, leaked: bool
    ) -> EvalReportRow:
        """A new **running** row (D378); its id is the report's."""
        item = PlannerEvalReport(
            campaign_ids=list(campaign_ids),
            holdout=holdout,
            leaked=leaked,
            report={RUN_STATE_KEY: {"status": STATUS_RUNNING}},
        )
        try:
            async with self._sessions() as db, db.begin():
                db.add(item)
                await db.flush()
                await db.refresh(item)
                return _row(item)
        except _DB_ERRORS as exc:
            raise self._error(exc, "write") from exc

    async def _replace_running(
        self, report_id: int, values: Mapping[str, Any]
    ) -> EvalReportRow | None:
        """``values`` on row ``report_id`` if it is still running; the row
        after, or None (no such row, or no longer running)."""
        query = (
            update(PlannerEvalReport)
            .where(
                PlannerEvalReport.id == report_id,
                PlannerEvalReport.report[RUN_STATE_KEY]["status"].astext
                == STATUS_RUNNING,
            )
            .values(**values)
            .returning(PlannerEvalReport)
        )
        try:
            async with self._sessions() as db, db.begin():
                found = (await db.execute(query)).scalar_one_or_none()
                return None if found is None else _row(found)
        except _DB_ERRORS as exc:
            raise self._error(exc, "write") from exc

    async def finish(
        self, report_id: int, report: Mapping[str, Any]
    ) -> EvalReportRow | None:
        """The running row ``report_id`` becomes the stored ``report``
        (``done``); its columns copied from the report, as :meth:`insert`."""
        return await self._replace_running(
            report_id,
            {
                "campaign_ids": list(report["campaign_ids"]),
                "holdout": report["holdout"],
                "leaked": bool(report["leaked"]),
                "report": dict(report),
            },
        )

    async def fail(
        self, report_id: int, reason: str, detail: str | None = None
    ) -> EvalReportRow | None:
        """The running row ``report_id`` becomes ``failed`` (``reason``)."""
        return await self._replace_running(
            report_id, {"report": failed_state(reason, detail)}
        )

    async def sweep_running(
        self,
        reason: str = CONTROLLER_RESTART,
        detail: str = "the controller stopped while this evaluation ran; run it again",
    ) -> int:
        """Fail every running row (the controller's start-up, D378); how
        many."""
        query = (
            update(PlannerEvalReport)
            .where(
                PlannerEvalReport.report[RUN_STATE_KEY]["status"].astext
                == STATUS_RUNNING
            )
            .values(report=failed_state(reason, detail))
            .returning(PlannerEvalReport.id)
        )
        try:
            async with self._sessions() as db, db.begin():
                return len(list((await db.execute(query)).scalars()))
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
                report[RUN_STATE_KEY],
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
                status=_state_of(r[8])[0],
                reason=_state_of(r[8])[1],
            )
            for r in rows
        ]


def _state_of(state: Any) -> tuple[str, str | None, str | None]:
    return run_state({} if state is None else {RUN_STATE_KEY: state})
