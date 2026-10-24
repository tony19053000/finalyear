"""``/v1/planner/eval-reports`` - the planner's offline evaluation (V0.14 G4, D297/D318).

* ``POST /v1/planner/eval-reports`` (**admin**): evaluate the given benchmark
  campaigns - every policy replayed on every labelled group, regret vs the
  best measured configuration, held-out calibrations (D316), the ML fold
  models (D368), bootstrap CIs and the criterion verdicts - and store the
  report. **Start-then-poll** (V0.15 G6b, D378): the evaluation runs in the
  background (minutes on a lab-sized dataset); the answer is ``202
  {report_id, status: running}``. The body must carry ``background: true``:
  a client from before the change (which expects the report in the answer)
  gets 400 ``eval_requires_background``, never a 202 it would misread.
  Unknown campaign -> 404; one already running -> 409
  ``eval_already_running`` (with its ``report_id``). ``compute planner
  eval``.
* ``GET /v1/planner/eval-reports[?limit=N]`` (any signed-in user): the newest
  reports as summaries (status, verdicts, evaluated groups; no per-group
  rows).
* ``GET /v1/planner/eval-reports/{id}[?format=markdown]`` (any signed-in
  user): one report - ``status`` ``running`` / ``done`` / ``failed`` (with
  ``reason``: ``controller_restart``, ``invalid_input``, ``not_found``,
  ``database``, ``crashed``) and, once done, the report (JSON) or its
  Markdown rendering (409 ``eval_report_not_done`` before).

Unknown report -> 404 (reason ``unknown_eval_report``); a request too large
(``holdout`` naming no evaluated family, too many records or a report over
the size cap) ends the report ``failed`` / ``invalid_input``; database down
-> 503 ``network_error``. The shape is
:mod:`~meshforge.controller.api.planner_eval_models` (D318).
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import PlainTextResponse

from meshforge.common.errors import FailureType
from meshforge.controller.api.auth import AdminUser, CurrentUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.planner_eval_models import (
    EvalReportListOut,
    EvalReportOut,
    EvalReportRecordOut,
    EvalReportSummaryOut,
    EvalRequestIn,
    EvalStartOut,
    VerdictOut,
)
from meshforge.controller.planner_eval.loader import EvalLoadError
from meshforge.controller.planner_eval.report import render_markdown
from meshforge.controller.planner_eval.service import (
    EvalBusyError,
    EvalNotFoundError,
    EvalService,
)
from meshforge.controller.planner_eval.store import (
    MAX_LIST,
    STATUS_DONE,
    EvalReportRow,
    EvalStoreError,
)

__all__ = [
    "EVAL_ALREADY_RUNNING_REASON",
    "EVAL_NOT_DONE_REASON",
    "EVAL_REQUIRES_BACKGROUND_REASON",
    "PLANNER_EVAL_STATE_KEY",
    "UNKNOWN_EVAL_REPORT_REASON",
    "PlannerEvalApi",
    "planner_eval_router",
]

PLANNER_EVAL_STATE_KEY: Final = "meshforge_planner_eval"
UNKNOWN_EVAL_REPORT_REASON: Final = "unknown_eval_report"
"""``error.reason`` of the 404 for a report id the store does not have. Lets a
client tell it from an unmatched route's 404 (both ``not_found``; D321): the
Planner page says "report not found" only for this one."""
EVAL_ALREADY_RUNNING_REASON: Final = "eval_already_running"
"""V0.15 G6b (D378): ``error.reason`` of the 409 for a second start."""
EVAL_REQUIRES_BACKGROUND_REASON: Final = "eval_requires_background"
"""V0.15 G6b (D378): ``error.reason`` of the 400 for a request without
``background: true`` (a client from before start-then-poll)."""
EVAL_NOT_DONE_REASON: Final = "eval_report_not_done"
"""V0.15 G6b (D378): ``error.reason`` of the 409 for the Markdown of a
report that is running or failed."""
MARKDOWN_MEDIA_TYPE: Final = "text/markdown; charset=utf-8"
_OLD_CLIENT_MESSAGE: Final = (
    "this controller runs planner evaluations in the background: POST "
    "/v1/planner/eval-reports answers 202 {report_id} and GET "
    "/v1/planner/eval-reports/{report_id} reports the progress. Send "
    '"background": true - or update the compute CLI / reload the dashboard'
)

planner_eval_router = APIRouter(prefix="/v1", tags=["planner"])


class PlannerEvalApi:
    """What the routes need (on ``app.state``)."""

    def __init__(self, *, service: EvalService) -> None:
        self.service = service


def get_planner_eval_api(request: Request) -> PlannerEvalApi:
    api: PlannerEvalApi | None = getattr(
        request.app.state, PLANNER_EVAL_STATE_KEY, None
    )
    if api is None:
        raise ApiError(
            FailureType.CONFIG_ERROR, "the planner evaluator is not configured"
        )
    return api


Api = Annotated[PlannerEvalApi, Depends(get_planner_eval_api)]


def _unavailable() -> ApiError:
    return ApiError(
        FailureType.NETWORK_ERROR,
        "the database is unavailable; try again",
        headers={"Retry-After": "2"},
    )


def record_out(row: EvalReportRow, service: EvalService) -> EvalReportRecordOut:
    run_status, reason, detail = service.status_of(row)
    return EvalReportRecordOut(
        id=row.id,
        created_at=row.created_at,
        campaign_ids=row.campaign_ids,
        holdout=row.holdout,
        leaked=row.leaked,
        status=run_status,  # type: ignore[arg-type]  # one of the three
        reason=reason,
        detail=detail,
        report=EvalReportOut.model_validate(row.report)
        if run_status == STATUS_DONE
        else None,
    )


@planner_eval_router.post(
    "/planner/eval-reports",
    response_model=EvalStartOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start evaluating benchmark campaigns: regret of every policy (admin)",
)
async def create_eval_report(
    admin: AdminUser, api: Api, body: EvalRequestIn
) -> EvalStartOut:
    """Start an evaluation in the background (module docstring)."""
    if body.background is not True:
        raise ApiError(
            FailureType.INVALID_INPUT,
            _OLD_CLIENT_MESSAGE,
            extra={"reason": EVAL_REQUIRES_BACKGROUND_REASON},
        )
    try:
        row = await api.service.start(
            body.campaign_ids,
            holdout=body.holdout,
            allow_leak=body.allow_leak,
            user_id=admin.user_id,
        )
    except EvalBusyError as exc:
        raise ApiError(
            FailureType.CONFLICT,
            str(exc),
            extra={
                "reason": EVAL_ALREADY_RUNNING_REASON,
                "report_id": exc.details.get("report_id"),
            },
        ) from None
    except EvalNotFoundError as exc:
        raise ApiError(FailureType.NOT_FOUND, str(exc)) from None
    except (EvalLoadError, EvalStoreError):
        raise _unavailable() from None
    return EvalStartOut(report_id=row.id, status="running")


@planner_eval_router.get(
    "/planner/eval-reports",
    response_model=EvalReportListOut,
    summary="The newest planner evaluation reports (summaries)",
)
async def list_eval_reports(
    _user: CurrentUser,
    api: Api,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
) -> EvalReportListOut:
    try:
        rows = await api.service.reports.recent(limit)
    except EvalStoreError:
        raise _unavailable() from None
    out = []
    for r in rows:
        run_status, reason = r.status, r.reason
        if run_status != STATUS_DONE:
            # the same overlay as one report's (never stuck running)
            run_status, reason, _detail = api.service.status_of(
                EvalReportRow(
                    id=r.id,
                    created_at=r.created_at,
                    campaign_ids=r.campaign_ids,
                    holdout=r.holdout,
                    leaked=r.leaked,
                    report={"run_state": {"status": run_status, "reason": reason}},
                )
            )
        out.append(
            EvalReportSummaryOut(
                id=r.id,
                created_at=r.created_at,
                campaign_ids=r.campaign_ids,
                holdout=r.holdout,
                leaked=r.leaked,
                eval_version=r.eval_version,
                evaluated_groups=r.evaluated_groups,
                verdicts=[VerdictOut.model_validate(v) for v in r.verdicts],
                status=run_status,  # type: ignore[arg-type]  # one of the three
                reason=reason,
            )
        )
    return EvalReportListOut(reports=out)


@planner_eval_router.get(
    "/planner/eval-reports/{report_id}",
    response_model=EvalReportRecordOut,
    summary="One planner evaluation report: its status, and once done the "
    "report (JSON or Markdown)",
    responses={
        200: {
            "content": {MARKDOWN_MEDIA_TYPE: {}},
            "description": "JSON (default) or, with format=markdown, the "
            "report rendered as Markdown (409 eval_report_not_done before "
            "it is done)",
        }
    },
)
async def get_eval_report(
    _user: CurrentUser,
    api: Api,
    report_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    format: Annotated[  # noqa: A002 - the query parameter's public name
        Literal["json", "markdown"], Query(description="json (default) or markdown.")
    ] = "json",
) -> Any:
    try:
        row = await api.service.reports.get(report_id)
    except EvalStoreError:
        raise _unavailable() from None
    if row is None:
        raise ApiError(
            FailureType.NOT_FOUND,
            f"no evaluation report {report_id}",
            extra={"reason": UNKNOWN_EVAL_REPORT_REASON},
        )
    record = record_out(row, api.service)
    if format == "markdown":
        if record.status != STATUS_DONE:
            raise ApiError(
                FailureType.CONFLICT,
                f"evaluation report {report_id} is {record.status}"
                + (f" ({record.reason})" if record.reason else "")
                + "; only a done report has a Markdown rendering",
                extra={"reason": EVAL_NOT_DONE_REASON, "status": record.status},
            )
        return PlainTextResponse(
            render_markdown(row.report), media_type=MARKDOWN_MEDIA_TYPE
        )
    return record
