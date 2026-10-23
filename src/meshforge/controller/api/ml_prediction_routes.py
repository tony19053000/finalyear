"""``GET /v1/planner/ml/predictions`` - predicted vs actual per term (V0.15
G5b, D374): the Models page's scatter.

**Admin only** (403 for a user): the points name every user's decisions and
nodes. ``?term=`` one learnable term, ``?set=`` only decisions planned with
that model set, ``?limit=`` the newest matched runs read (default 500, at
most 2000 - the bound; filters apply within them). Each point: the decision,
node, family, the analytical / calibrated / ML seconds, whether ML was used
and why not, the actual and the model set
(:mod:`meshforge.controller.ml.predictions`). Read from the decision JSON -
no migration (Q29). Database down -> 503 ``network_error``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.ml_model_routes import SET_VERSION_PATTERN
from meshforge.controller.api.models import ApiModel
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.ml.predictions import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PredictionsError,
    read_predictions,
)

__all__ = [
    "ML_PREDICTIONS_STATE_KEY",
    "MlPredictionPointOut",
    "MlPredictionsApi",
    "MlPredictionsOut",
    "ml_predictions_router",
]

log = get_logger(__name__)

ML_PREDICTIONS_STATE_KEY: Final = "meshforge_ml_predictions"

ml_predictions_router = APIRouter(prefix="/v1", tags=["planner"])

type MlTerm = Literal[
    "transfer",
    "env",
    "compute",
    "output",
    "lane_stage",
    "lane_env",
    "chunk_rate",
    "combine",
]
"""``planner/predictor.ML_TERMS`` (a unit test holds the two equal)."""


@dataclass(frozen=True, slots=True)
class MlPredictionsApi:
    """What the route needs (on ``app.state``)."""

    engine: AsyncEngine
    calibration: CalibrationStore


def get_ml_predictions_api(request: Request) -> MlPredictionsApi:
    api: MlPredictionsApi | None = getattr(
        request.app.state, ML_PREDICTIONS_STATE_KEY, None
    )
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "ML predictions are not configured")
    return api


class MlPredictionPointOut(ApiModel):
    decision_id: int
    term: str
    node_id: str
    family: str | None
    analytical_s: float
    """The raw analytical prediction (pre-calibration, pre-ML); a
    ``chunk_rate`` is seconds per item."""
    calibrated_s: float | None
    """What the calibration predicted, or would have where ML corrected the
    term (the decision's calibration version re-applied); null = that version
    cannot be read."""
    ml_s: float | None
    """``analytical_s x e^y_hat`` where ML corrected the term, else null."""
    used: bool
    reason: str | None
    """Why ML did not correct the term (null when used, or no model)."""
    actual_s: float
    ml_model_version: str | None
    """The model set the decision was planned with (null = none)."""


class MlPredictionsOut(ApiModel):
    limit: int
    decisions_read: int
    """Matched runs read (the newest ``limit``); the points are their
    learnable terms with an honest actual, after the filters."""
    term: str | None
    set: str | None
    points: list[MlPredictionPointOut]


@ml_predictions_router.get(
    "/planner/ml/predictions",
    response_model=MlPredictionsOut,
    summary="Predicted vs actual per term (analytical, calibrated, ML) - admin only",
)
async def get_ml_predictions(
    admin: AdminUser,
    api: Annotated[MlPredictionsApi, Depends(get_ml_predictions_api)],
    term: Annotated[MlTerm | None, Query(description="One learnable term.")] = None,
    set_version: Annotated[
        str | None,
        Query(
            alias="set",
            pattern=SET_VERSION_PATTERN,
            description="Only decisions planned with this model set.",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="Newest matched runs read."),
    ] = DEFAULT_LIMIT,
) -> MlPredictionsOut:
    try:
        read = await read_predictions(
            api.engine,
            api.calibration,
            limit=limit,
            term=term,
            set_version=set_version,
        )
    except PredictionsError:
        raise ApiError(
            FailureType.NETWORK_ERROR,
            "the database is unavailable; retry",
            headers={"Retry-After": "2"},
        ) from None
    log.info(
        "controller.ml.predictions_served",
        user_id=admin.user_id,
        decisions=read.decisions_read,
        points=len(read.points),
        term=term,
    )
    return MlPredictionsOut(
        limit=limit,
        decisions_read=read.decisions_read,
        term=term,
        set=set_version,
        points=[
            MlPredictionPointOut(
                decision_id=p.decision_id,
                term=p.term,
                node_id=p.node_id,
                family=p.family,
                analytical_s=p.analytical_s,
                calibrated_s=p.calibrated_s,
                ml_s=p.ml_s,
                used=p.used,
                reason=p.reason,
                actual_s=p.actual_s,
                ml_model_version=p.ml_model_version,
            )
            for p in read.points
        ],
    )
