"""``GET /v1/planner/ml/dataset`` - the ML training dataset (V0.15 G1, D333).

**Admin only** (403 for a user): the rows name every user's jobs and nodes
(never a secret value - the dataset holds ids, numbers and family names).

* default (``format=parquet``): the dataset as one Parquet file
  (:mod:`meshforge.controller.ml.dataset`; the same matched runs give the
  same bytes), with its digest in ``X-MeshForge-Dataset-Digest``;
* ``format=summary``: the JSON sidecar only - versions, digest, counts (rows
  per term / family, skips by reason), the read cap and whether it cut older
  runs, and the schema.

The dataset is built per request from at most ``[controller.planner]
ml_dataset_max_runs`` newest matched runs (bounded paged reads; the pure work
runs in a worker thread). Database down -> 503 ``network_error``
(``Retry-After``). ``compute planner ml dataset`` calls this route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel

__all__ = [
    "DIGEST_HEADER",
    "ML_STATE_KEY",
    "MlDatasetApi",
    "MlDatasetSummaryOut",
    "ml_router",
]

log = get_logger(__name__)

ML_STATE_KEY: Final = "meshforge_ml"
PARQUET_MEDIA_TYPE: Final = "application/vnd.apache.parquet"
DIGEST_HEADER: Final = "X-MeshForge-Dataset-Digest"

ml_router = APIRouter(prefix="/v1", tags=["planner"])


@dataclass(frozen=True, slots=True)
class MlDatasetApi:
    """What the route needs (on ``app.state``)."""

    engine: AsyncEngine
    max_runs: int


def get_ml_api(request: Request) -> MlDatasetApi:
    api: MlDatasetApi | None = getattr(request.app.state, ML_STATE_KEY, None)
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "the ML dataset is not configured")
    return api


class MlCountsOut(ApiModel):
    runs: int
    """Matched runs read."""
    runs_with_rows: int
    rows: int
    rows_by_term: dict[str, int]
    rows_by_family: dict[str, int]
    """``(none)`` = rows without a workload family."""
    skipped: dict[str, int]
    """Skip reason -> count (decisions for ``missing_replay`` /
    ``unreadable_replay`` / ``unmatched``, terms for the rest)."""


class MlColumnOut(ApiModel):
    name: str
    type: str
    nullable: bool
    group: str | None
    unit: str | None


class MlFeatureOut(ApiModel):
    name: str
    kind: str
    unit: str
    group: str
    doc: str


class MlTermOut(ApiModel):
    name: str
    scope: str
    planner_term: str
    phase: str | None
    actual: str


class MlSchemaOut(ApiModel):
    fingerprint: str
    columns: list[MlColumnOut]
    features: list[MlFeatureOut]
    terms: list[MlTermOut]


class MlDatasetSummaryOut(ApiModel):
    """The dataset's JSON sidecar (``?format=summary``)."""

    dataset_version: int
    feature_version: int
    digest: str
    """``sha256:`` + the SHA-256 of the Parquet bytes."""
    bytes: int
    rows: int
    max_runs: int
    truncated: bool
    """Older matched runs exist beyond ``max_runs``."""
    counts: MlCountsOut
    dataset_schema: MlSchemaOut


@ml_router.get(
    "/planner/ml/dataset",
    summary="The ML training dataset (Parquet, or its summary) - admin only",
    response_model=MlDatasetSummaryOut,
    responses={
        200: {
            "content": {PARQUET_MEDIA_TYPE: {}},
            "description": "format=parquet: the dataset file; format=summary: JSON",
        }
    },
)
async def get_ml_dataset(
    admin: AdminUser,
    api: Annotated[MlDatasetApi, Depends(get_ml_api)],
    format: Annotated[  # noqa: A002 - the query parameter's public name
        Literal["parquet", "summary"],
        Query(description="parquet = the file (default); summary = the sidecar."),
    ] = "parquet",
) -> Response | MlDatasetSummaryOut:
    from meshforge.controller.ml.export import build_dataset, summary
    from meshforge.controller.ml.loader import MlLoadError

    try:
        built = await build_dataset(api.engine, max_runs=api.max_runs)
    except MlLoadError:
        raise ApiError(
            FailureType.NETWORK_ERROR,
            "the database is unavailable; retry",
            headers={"Retry-After": "2"},
        ) from None
    log.info(
        "controller.ml.dataset_served",
        user_id=admin.user_id,
        format=format,
        rows=built.dataset.counts["rows"],
        digest=built.dataset.digest,
    )
    if format == "summary":
        return MlDatasetSummaryOut.model_validate(summary(built))
    return Response(
        content=built.dataset.parquet,
        media_type=PARQUET_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="meshforge-ml-dataset.parquet"',
            DIGEST_HEADER: built.dataset.digest,
        },
    )
