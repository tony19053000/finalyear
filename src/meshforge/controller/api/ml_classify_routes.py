"""``POST /v1/planner/ml/classifier-examples`` - the 0.15.10 classifier
experiment's examples (V0.15 G6a, D343, D375, D376).

**Admin only** (403 for a user): the examples name every campaign group and
configuration. For the given benchmark campaigns the controller turns every
labelled group into one example - the job and cluster features, the winning
configuration class, and per class the regret of the planner's best
candidate of that class (re-planned with the evaluator's held-out
calibration; :mod:`meshforge.controller.planner_eval.classifier_examples`).
Nothing is stored and no model is trained here: ``compute planner ml
classify`` fetches the document and trains + evaluates the classifiers in
the ``ml`` child on the operator's machine (report only - a classifier is
never wired into the planner). Unknown campaign -> 404; too many records ->
400 ``invalid_input``; database down -> 503 ``network_error``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from meshforge.common.errors import FailureType
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel
from meshforge.controller.api.planner_eval_models import MAX_EVAL_CAMPAIGNS
from meshforge.controller.calibration_store import CalibrationStoreError
from meshforge.controller.planner_eval.classifier_service import (
    ClassifierExamplesService,
)
from meshforge.controller.planner_eval.loader import EvalLoadError, EvalTooLargeError
from meshforge.controller.planner_eval.service import EvalNotFoundError

__all__ = [
    "ML_CLASSIFY_STATE_KEY",
    "ClassifierExamplesIn",
    "ClassifierExamplesOut",
    "MlClassifyApi",
    "ml_classify_router",
]

ML_CLASSIFY_STATE_KEY: Final = "meshforge_ml_classify"
_ID_MAX: Final = 64

ml_classify_router = APIRouter(prefix="/v1", tags=["planner"])


@dataclass(frozen=True, slots=True)
class MlClassifyApi:
    """What the route needs (on ``app.state``)."""

    service: ClassifierExamplesService


def get_ml_classify_api(request: Request) -> MlClassifyApi:
    api: MlClassifyApi | None = getattr(request.app.state, ML_CLASSIFY_STATE_KEY, None)
    if api is None:
        raise ApiError(
            FailureType.CONFIG_ERROR, "the classifier experiment is not configured"
        )
    return api


class ClassifierExamplesIn(ApiModel):
    campaign_ids: list[Annotated[str, Field(min_length=1, max_length=_ID_MAX)]] = Field(
        min_length=1,
        max_length=MAX_EVAL_CAMPAIGNS,
        description="The benchmark campaigns whose labelled groups become examples.",
    )


class ClassOutcomeOut(ApiModel):
    config_id: str | None
    """The configuration the pick maps to (null = none)."""
    status: str
    """``counted`` | ``unmeasured`` | ``no_replay`` (the evaluator's pick
    statuses)."""
    regret_pct: float | None
    votes: dict[str, int]
    """Configuration id (or ``unmeasured``) -> records that picked it."""


class ClassifierExampleOut(ApiModel):
    campaign_id: str
    group_id: str
    family: str
    fingerprint: str | None
    label: str
    """The winning class, mapped to one the planner enumerated."""
    oracle_class: str
    """The oracle configuration's own class."""
    records_used: int
    features: list[float | None]
    """In ``feature_names`` order; null = missing."""
    classes: dict[str, ClassOutcomeOut]
    """Class -> the planner's best candidate of that class."""
    cost_model: ClassOutcomeOut
    """The planner's own pick (the cost-regression row)."""


class ClassifierCalibrationOut(ApiModel):
    source: str
    fitted_runs: int
    excluded_runs: int


class ClassifierExamplesOut(ApiModel):
    examples_version: int
    campaign_ids: list[str]
    policy: str
    """The planner row the classes are planned with (calibrated
    ``cost_model``, ``min_time``)."""
    feature_names: list[str]
    counts: dict[str, int]
    """Groups by why they have no example; ``record:<reason>`` = records
    skipped."""
    calibrations: dict[str, ClassifierCalibrationOut]
    """Family -> its held-out calibration refit (D316)."""
    examples: list[ClassifierExampleOut]


@ml_classify_router.post(
    "/planner/ml/classifier-examples",
    response_model=ClassifierExamplesOut,
    summary="The classifier experiment's examples (labelled groups) - admin only",
)
async def post_classifier_examples(
    admin: AdminUser,
    body: ClassifierExamplesIn,
    api: Annotated[MlClassifyApi, Depends(get_ml_classify_api)],
) -> ClassifierExamplesOut:
    try:
        doc = await api.service.document(body.campaign_ids, user_id=admin.user_id)
    except EvalNotFoundError as exc:
        raise ApiError(FailureType.NOT_FOUND, str(exc)) from None
    except EvalTooLargeError as exc:
        raise ApiError(FailureType.INVALID_INPUT, str(exc)) from None
    except (EvalLoadError, CalibrationStoreError):
        raise ApiError(
            FailureType.NETWORK_ERROR,
            "the database is unavailable; retry",
            headers={"Retry-After": "2"},
        ) from None
    return ClassifierExamplesOut.model_validate(doc)
