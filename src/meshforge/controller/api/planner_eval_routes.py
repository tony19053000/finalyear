"""``/v1/planner/eval-reports`` - the planner's offline evaluation (V0.14 G4, D297/D318).

* ``POST /v1/planner/eval-reports`` (**admin**): evaluate the given benchmark
  campaigns now - every policy replayed on every labelled group, regret vs the
  best measured configuration, held-out calibrations (D316), bootstrap CIs and
  the criterion verdicts - store the report and answer it (201).
  ``compute planner eval``.
* ``GET /v1/planner/eval-reports[?limit=N]`` (any signed-in user): the newest
  reports as summaries (verdicts, evaluated groups; no per-group rows).
* ``GET /v1/planner/eval-reports/{id}[?format=markdown]`` (any signed-in
  user): one full report (JSON), or its Markdown rendering.

Unknown campaign / report -> 404; ``holdout`` naming no evaluated family, too
many records or a report over the size cap -> 400 ``invalid_input``; database
down -> 503 ``network_error``. The shape is
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
    VerdictOut,
)
from meshforge.controller.calibration_store import CalibrationStoreError
from meshforge.controller.planner_eval.loader import EvalLoadError, EvalTooLargeError
from meshforge.controller.planner_eval.report import render_markdown
from meshforge.controller.planner_eval.service import (
    EvalInputError,
    EvalNotFoundError,
    EvalService,
)
from meshforge.controller.planner_eval.store import (
    MAX_LIST,
    EvalReportRow,
    EvalStoreError,
)

__all__ = ["PLANNER_EVAL_STATE_KEY", "PlannerEvalApi", "planner_eval_router"]

PLANNER_EVAL_STATE_KEY: Final = "meshforge_planner_eval"
MARKDOWN_MEDIA_TYPE: Final = "text/markdown; charset=utf-8"

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


def record_out(row: EvalReportRow) -> EvalReportRecordOut:
    return EvalReportRecordOut(
        id=row.id,
        created_at=row.created_at,
        campaign_ids=row.campaign_ids,
        holdout=row.holdout,
        leaked=row.leaked,
        report=EvalReportOut.model_validate(row.report),
    )


@planner_eval_router.post(
    "/planner/eval-reports",
    response_model=EvalReportRecordOut,
    status_code=status.HTTP_201_CREATED,
    summary="Evaluate benchmark campaigns: regret of every policy (admin)",
)
async def create_eval_report(
    admin: AdminUser, api: Api, body: EvalRequestIn
) -> EvalReportRecordOut:
    """Evaluate and store a report (module docstring)."""
    try:
        row = await api.service.run(
            body.campaign_ids,
            holdout=body.holdout,
            allow_leak=body.allow_leak,
            user_id=admin.user_id,
        )
    except EvalNotFoundError as exc:
        raise ApiError(FailureType.NOT_FOUND, str(exc)) from None
    except (EvalInputError, EvalTooLargeError) as exc:
        raise ApiError(FailureType.INVALID_INPUT, str(exc)) from None
    except (EvalLoadError, EvalStoreError, CalibrationStoreError):
        raise _unavailable() from None
    return record_out(row)


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
    return EvalReportListOut(
        reports=[
            EvalReportSummaryOut(
                id=r.id,
                created_at=r.created_at,
                campaign_ids=r.campaign_ids,
                holdout=r.holdout,
                leaked=r.leaked,
                eval_version=r.eval_version,
                evaluated_groups=r.evaluated_groups,
                verdicts=[VerdictOut.model_validate(v) for v in r.verdicts],
            )
            for r in rows
        ]
    )


@planner_eval_router.get(
    "/planner/eval-reports/{report_id}",
    response_model=EvalReportRecordOut,
    summary="One planner evaluation report (JSON or Markdown)",
    responses={
        200: {
            "content": {MARKDOWN_MEDIA_TYPE: {}},
            "description": "JSON (default) or, with format=markdown, the "
            "report rendered as Markdown",
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
        raise ApiError(FailureType.NOT_FOUND, f"no evaluation report {report_id}")
    if format == "markdown":
        return PlainTextResponse(
            render_markdown(row.report), media_type=MARKDOWN_MEDIA_TYPE
        )
    return record_out(row)
