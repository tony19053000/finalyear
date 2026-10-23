"""``/v1/planner/ml/...`` - the ML model registry (V0.15 G3, D339, D358-D361).

* ``GET /v1/planner/ml/models[?limit=N]`` (any signed-in user): the newest
  model sets (status, reason, members with algorithm / metrics / reason),
  what the planner may use now (``mode`` ``auto`` / ``off`` / ``pinned``,
  the serving set, why it is not loaded) and the retrain task's state.
  ``compute planner ml models``.
* ``GET /v1/planner/ml/models/{set_version}``: one set in full - plus its
  activation-gate document and training inputs.
* ``POST /v1/planner/ml/train`` (**admin**): start a retrain now (in the
  background; ``GET /v1/planner/ml/train`` reports it). 409 when the
  controller has no ``ml`` extra. ``compute planner ml train [--wait]``.
* ``POST /v1/planner/ml/models/{set_version}/pin`` / ``.../unpin``
  (**admin**): the override and rollback (a ``rejected`` set or one whose
  artefacts are gone -> 409). ``compute planner ml pin|unpin``.

V0.15 G5b (G4 reviewer): ``in_use`` also carries the dispatcher's live ML
numbers - ``ml_predict_last_ms`` (the last ``predict`` call, wall clock) and
``ml_predictor_errors_total`` (decisions that fell back because the predictor
failed, D363).

Database down -> 503 ``network_error``. An unknown set -> 404.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Request

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser, CurrentUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.jobs.planning import MlServingStats
from meshforge.controller.ml.registry import (
    MlRegistryError,
    ModelRegistry,
    PinError,
    SetRow,
)
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.ml_retrain_task import MlRetrainer, RetrainResult

__all__ = ["ML_MODELS_STATE_KEY", "MlModelsApi", "ml_models_router"]

log = get_logger(__name__)

ML_MODELS_STATE_KEY: Final = "meshforge_ml_models"
SET_VERSION_PATTERN: Final = r"^sha256:[0-9a-f]{64}$"

ml_models_router = APIRouter(prefix="/v1", tags=["planner"])

type SetVersion = Annotated[
    str, Path(pattern=SET_VERSION_PATTERN, description="sha256:<64 hex>")
]


class MlModelsApi:
    """What the routes need (on ``app.state``)."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        source: ModelSource,
        retrainer: MlRetrainer,
        serving_stats: Callable[[], MlServingStats] | None = None,
    ) -> None:
        self.registry = registry
        self.source = source
        self.retrainer = retrainer
        self.serving_stats = serving_stats
        """V0.15 G5b: the dispatcher's live ML numbers (None = no
        dispatcher in this process: the fields are null)."""


def get_ml_models_api(request: Request) -> MlModelsApi:
    api: MlModelsApi | None = getattr(request.app.state, ML_MODELS_STATE_KEY, None)
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "the model registry is not configured")
    return api


class MlMemberOut(ApiModel):
    term: str
    algorithm: str
    """A learned algorithm, or ``none`` (the term has no model in the set)."""
    version: str
    params: dict[str, Any]
    metrics: dict[str, Any]
    reason: str | None
    artefact_bytes: int | None


class MlSetOut(ApiModel):
    set_version: str
    created_at: Utc
    status_changed_at: Utc
    status: str
    """``active`` | ``rejected`` | ``superseded`` | ``pinned``."""
    reason: str | None
    dataset_digest: str
    feature_version: int
    members: list[MlMemberOut]


class MlSetDetailOut(MlSetOut):
    gate: dict[str, Any]
    """The activation gate's numbers; ``regret`` is the D319 regret block (G5a), or
    ``"pending_g4"`` on sets gated before V0.15 G5."""
    inputs: dict[str, Any]


class MlInUseOut(ApiModel):
    mode: str
    """``auto`` | ``off`` | ``pinned`` (``[controller.planner] ml``)."""
    pinned_config: str | None
    """The set ``ml = "<set_version>"`` pins (None with auto / off)."""
    kind: str | None
    """What :attr:`in_use_set` is: ``pinned`` / ``active`` (auto) or
    ``config``."""
    in_use_set: str | None
    """The set loaded for planning (None = none: calibration only)."""
    unavailable: str | None
    """Why the serving set is not loaded (``artefact_missing``,
    ``library_missing``, ...)."""
    ml_predict_last_ms: float | None = None
    """V0.15 G5b: the dispatcher's last ``predict`` call (wall clock, ms;
    null = no model or not asked yet). The ``ml_predict_budget_ms`` budget is
    wall clock too: a model near it can flip a decision to
    ``predictor_error``."""
    ml_predictor_errors_total: int | None = None
    """V0.15 G5b (D363): decisions that fell back to calibration because the
    predictor failed, since the controller started (null = no dispatcher)."""


class MlRetrainOut(ApiModel):
    trigger: str
    started_at: Utc
    finished_at: Utc
    created_set: str | None
    status: str | None
    reason: str | None
    skipped: str | None
    error: str | None
    detail: str | None
    superseded_set: str | None
    rows: int | None
    seconds: dict[str, Any]


class MlRetrainStateOut(ApiModel):
    available: bool
    """False = the controller has no ``ml`` extra (``missing`` lists it)."""
    missing: list[str]
    running: bool
    running_since: Utc | None
    retrains: int
    failures: int
    last: MlRetrainOut | None
    interval_s: float


class MlModelsOut(ApiModel):
    in_use: MlInUseOut
    serving_set: str | None
    """Pinned, else active (what ``auto`` uses)."""
    sets: list[MlSetOut]
    retrain: MlRetrainStateOut


class MlTrainOut(ApiModel):
    started: bool
    """False = a retrain was already running (this request joined nothing)."""
    retrain: MlRetrainStateOut


def _members(row: SetRow) -> list[MlMemberOut]:
    return [
        MlMemberOut(
            term=m.term,
            algorithm=m.algorithm,
            version=m.version,
            params=m.params,
            metrics=m.metrics,
            reason=m.reason,
            artefact_bytes=m.artefact_bytes,
        )
        for m in row.members
    ]


def _set(row: SetRow) -> MlSetOut:
    return MlSetOut(
        set_version=row.set_version,
        created_at=row.created_at,
        status_changed_at=row.status_changed_at,
        status=row.status,
        reason=row.reason,
        dataset_digest=row.dataset_digest,
        feature_version=row.feature_version,
        members=_members(row),
    )


def _detail(row: SetRow) -> MlSetDetailOut:
    return MlSetDetailOut(
        **_set(row).model_dump(),
        gate=row.gate,
        inputs=row.inputs,
    )


def _in_use(source: ModelSource, api: MlModelsApi | None = None) -> MlInUseOut:
    current = source.current
    stats = None if api is None or api.serving_stats is None else api.serving_stats()
    return MlInUseOut(
        mode=source.mode,
        pinned_config=source.pinned,
        kind=source.kind,
        in_use_set=None
        if source.mode == "off" or current is None
        else current.set_version,
        unavailable=source.unavailable,
        ml_predict_last_ms=None if stats is None else stats.predict_last_ms,
        ml_predictor_errors_total=None
        if stats is None
        else stats.predictor_errors_total,
    )


def _result(result: RetrainResult | None) -> MlRetrainOut | None:
    if result is None:
        return None
    row = result.row
    return MlRetrainOut(
        trigger=result.trigger,
        started_at=result.started_at,
        finished_at=result.finished_at,
        created_set=None if row is None else row.set_version,
        status=None if row is None else row.status,
        reason=None if row is None else row.reason,
        skipped=result.skipped,
        error=result.error,
        detail=result.detail,
        superseded_set=result.superseded,
        rows=result.rows,
        seconds=result.seconds,
    )


def _retrain(api: MlModelsApi) -> MlRetrainStateOut:
    task = api.retrainer
    return MlRetrainStateOut(
        available=task.available,
        missing=list(task.missing),
        running=task.running,
        running_since=task.running_since,
        retrains=task.retrains,
        failures=task.failures,
        last=_result(task.last),
        interval_s=task.interval_s,
    )


def _unavailable(_exc: MlRegistryError) -> ApiError:
    return ApiError(
        FailureType.NETWORK_ERROR,
        "the database is unavailable; try again",
        headers={"Retry-After": "2"},
    )


@ml_models_router.get("/planner/ml/models", response_model=MlModelsOut)
async def get_models(
    _user: CurrentUser,
    api: Annotated[MlModelsApi, Depends(get_ml_models_api)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> MlModelsOut:
    """The newest model sets and what the planner may use now."""
    try:
        rows = await api.registry.recent(limit)
        _kind, serving = await api.registry.serving_version()
    except MlRegistryError as exc:
        raise _unavailable(exc) from None
    return MlModelsOut(
        in_use=_in_use(api.source, api),
        serving_set=serving,
        sets=[_set(r) for r in rows],
        retrain=_retrain(api),
    )


@ml_models_router.get("/planner/ml/models/{set_version}", response_model=MlSetDetailOut)
async def get_model_set(
    _user: CurrentUser,
    api: Annotated[MlModelsApi, Depends(get_ml_models_api)],
    set_version: SetVersion,
) -> MlSetDetailOut:
    """One model set with its gate document and training inputs."""
    try:
        row = await api.registry.get(set_version)
    except MlRegistryError as exc:
        raise _unavailable(exc) from None
    if row is None:
        raise ApiError(FailureType.NOT_FOUND, f"no model set {set_version}")
    return _detail(row)


@ml_models_router.get("/planner/ml/train", response_model=MlRetrainStateOut)
async def get_train(
    _user: CurrentUser,
    api: Annotated[MlModelsApi, Depends(get_ml_models_api)],
) -> MlRetrainStateOut:
    """The retrain task's state and its last result."""
    return _retrain(api)


@ml_models_router.post("/planner/ml/train", response_model=MlTrainOut)
async def train(
    admin: AdminUser,
    api: Annotated[MlModelsApi, Depends(get_ml_models_api)],
) -> MlTrainOut:
    """Start a retrain now (admin; runs in the background)."""
    if not api.retrainer.available:
        raise ApiError(
            FailureType.CONFLICT,
            "this controller cannot train models: the ml extra is not installed "
            f"(missing {', '.join(api.retrainer.missing)})",
        )
    started = api.retrainer.start("manual")
    log.info(
        "controller.planner.ml_train_requested",
        user_id=admin.user_id,
        started=started,
    )
    return MlTrainOut(started=started, retrain=_retrain(api))


async def _pin(api: MlModelsApi, set_version: str, *, pin: bool) -> MlSetDetailOut:
    try:
        row = await (
            api.registry.pin(set_version) if pin else api.registry.unpin(set_version)
        )
    except PinError as exc:
        raise ApiError(exc.failure_type, exc.message) from None
    except MlRegistryError as exc:
        raise _unavailable(exc) from None
    await api.source.refresh()
    return _detail(row)


@ml_models_router.post(
    "/planner/ml/models/{set_version}/pin", response_model=MlSetDetailOut
)
async def pin_set(
    admin: AdminUser,
    api: Annotated[MlModelsApi, Depends(get_ml_models_api)],
    set_version: SetVersion,
) -> MlSetDetailOut:
    """Pin a model set: the planner uses it until unpinned (admin)."""
    out = await _pin(api, set_version, pin=True)
    log.info(
        "controller.planner.ml_set_pinned",
        user_id=admin.user_id,
        ml_set_version=set_version,
        at=dt.datetime.now(dt.UTC).isoformat(),
    )
    return out


@ml_models_router.post(
    "/planner/ml/models/{set_version}/unpin", response_model=MlSetDetailOut
)
async def unpin_set(
    admin: AdminUser,
    api: Annotated[MlModelsApi, Depends(get_ml_models_api)],
    set_version: SetVersion,
) -> MlSetDetailOut:
    """Unpin a model set (admin)."""
    out = await _pin(api, set_version, pin=False)
    log.info(
        "controller.planner.ml_set_unpinned",
        user_id=admin.user_id,
        ml_set_version=set_version,
    )
    return out
