"""``/v1/planner/calibration`` - the planner's calibration (V0.14 G2, D293/D301/D306).

* ``POST /v1/planner/calibrate`` (**admin**): fit a new version now, gate it
  against the active one on the held-out runs (D306) and store it as
  ``active`` or ``rejected``; answers what was written (``created`` null and
  ``skipped`` = ``too_few_runs`` / ``nothing_to_fit`` / ``no_new_runs``
  when nothing was).
  ``compute planner calibrate``.
* ``GET /v1/planner/calibration[?version=N]`` (any signed-in user): one
  version's factors (default: the active one), each with ``n`` and its key
  level, plus what the planner uses now (``mode`` = ``auto`` / ``off`` /
  ``pinned``, ``in_use_version``) and the latest versions' summaries.
  ``compute planner calibration``.

Database down -> 503 ``network_error``. An unknown ``version`` -> 404.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser, CurrentUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.calibration_store import (
    CalibrationRow,
    CalibrationSource,
    CalibrationStore,
    CalibrationStoreError,
    factor_rows,
)
from meshforge.controller.planner.calibration import NOT_CALIBRATED, TERM_PHASE
from meshforge.controller.planner_calibration_task import CalibrationRefresher

__all__ = ["PLANNER_STATE_KEY", "PlannerApi", "planner_router"]

log = get_logger(__name__)

PLANNER_STATE_KEY: Final = "meshforge_planner"
MAX_RECENT: Final = 20

planner_router = APIRouter(prefix="/v1", tags=["planner"])


class PlannerApi:
    """What the routes need (on ``app.state``)."""

    def __init__(
        self,
        *,
        store: CalibrationStore,
        source: CalibrationSource,
        refresher: CalibrationRefresher,
    ) -> None:
        self.store = store
        self.source = source
        self.refresher = refresher


def get_planner_api(request: Request) -> PlannerApi:
    api: PlannerApi | None = getattr(request.app.state, PLANNER_STATE_KEY, None)
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "the planner API is not configured")
    return api


class FactorOut(ApiModel):
    term: str
    key_level: str
    node_id: str | None
    family: str | None
    factor: float
    n: int
    median: float


class CalibrationOut(ApiModel):
    version: int
    created_at: Utc
    status: str
    holdout_error_new: float | None
    holdout_error_active: float | None
    reason: str | None
    fit_inputs: dict[str, Any]
    readable: bool
    """False = its factors could not be read (never used for planning)."""
    factors: list[FactorOut]
    families: dict[str, str]


class CalibrationSummaryOut(ApiModel):
    version: int
    created_at: Utc
    status: str
    holdout_error_new: float | None
    holdout_error_active: float | None
    reason: str | None


class InUseOut(ApiModel):
    mode: str
    """``auto`` | ``off`` | ``pinned`` (``[controller.planner] calibration``)."""
    pinned_version: int | None
    in_use_version: int | None
    """The version the dispatcher planned its last tick with (None = none)."""


class CalibrationViewOut(ApiModel):
    in_use: InUseOut
    active_version: int | None
    calibration: CalibrationOut | None
    term_phase: dict[str, str]
    not_calibrated: dict[str, str]
    recent: list[CalibrationSummaryOut]


class CalibrateOut(ApiModel):
    matched_runs: int
    created: CalibrationOut | None
    skipped: str | None
    superseded_version: int | None
    in_use: InUseOut


def _full(row: CalibrationRow) -> CalibrationOut:
    calibration = row.calibration
    return CalibrationOut(
        version=row.version,
        created_at=row.created_at,
        status=row.status,
        holdout_error_new=row.holdout_error_new,
        holdout_error_active=row.holdout_error_active,
        reason=row.reason,
        fit_inputs=row.fit_inputs,
        readable=calibration is not None,
        factors=[]
        if calibration is None
        else [FactorOut(**item) for item in factor_rows(calibration)],
        families={} if calibration is None else dict(calibration.families),
    )


def _summary(row: CalibrationRow) -> CalibrationSummaryOut:
    return CalibrationSummaryOut(
        version=row.version,
        created_at=row.created_at,
        status=row.status,
        holdout_error_new=row.holdout_error_new,
        holdout_error_active=row.holdout_error_active,
        reason=row.reason,
    )


def _in_use(api: PlannerApi) -> InUseOut:
    current = api.source.current
    return InUseOut(
        mode=api.source.mode,
        pinned_version=api.source.pinned,
        in_use_version=None
        if api.source.mode == "off" or current is None
        else current.version,
    )


def _unavailable(_exc: CalibrationStoreError) -> ApiError:
    return ApiError(
        FailureType.NETWORK_ERROR,
        "the database is unavailable; try again",
        headers={"Retry-After": "2"},
    )


@planner_router.get("/planner/calibration", response_model=CalibrationViewOut)
async def get_calibration(
    _user: CurrentUser,
    api: Annotated[PlannerApi, Depends(get_planner_api)],
    version: Annotated[
        int | None,
        Query(
            ge=1, le=2**63 - 1, description="This version (default: the active one)."
        ),
    ] = None,
) -> CalibrationViewOut:
    """One calibration version's factors and what the planner uses now."""
    try:
        active = await api.store.active_row()
        row = active if version is None else await api.store.row(version)
        recent = await api.store.recent(MAX_RECENT)
    except CalibrationStoreError as exc:
        raise _unavailable(exc) from None
    if version is not None and row is None:
        raise ApiError(FailureType.NOT_FOUND, f"no calibration version {version}")
    return CalibrationViewOut(
        in_use=_in_use(api),
        active_version=None if active is None else active.version,
        calibration=None if row is None else _full(row),
        term_phase=dict(TERM_PHASE),
        not_calibrated=dict(NOT_CALIBRATED),
        recent=[_summary(r) for r in recent],
    )


@planner_router.post("/planner/calibrate", response_model=CalibrateOut)
async def calibrate(
    admin: AdminUser,
    api: Annotated[PlannerApi, Depends(get_planner_api)],
) -> CalibrateOut:
    """Fit, gate and store a new calibration version now (admin)."""
    started = dt.datetime.now(dt.UTC)
    try:
        result = await api.refresher.refresh_now()
    except CalibrationStoreError as exc:
        raise _unavailable(exc) from None
    log.info(
        "controller.planner.calibrate_requested",
        user_id=admin.user_id,
        created_version=None if result.row is None else result.row.version,
        status=None if result.row is None else result.row.status,
        took_s=round((dt.datetime.now(dt.UTC) - started).total_seconds(), 3),
    )
    return CalibrateOut(
        matched_runs=result.matched_runs,
        created=None if result.row is None else _full(result.row),
        skipped=result.skipped,
        superseded_version=result.superseded,
        in_use=_in_use(api),
    )
