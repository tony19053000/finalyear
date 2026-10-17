"""``GET /v1/telemetry/export`` - the telemetry dataset as Parquet (V0.9 G1, D211/D222).

**Admin only** (403 for a user): the records name every user's jobs, nodes and
benchmark parameters (never a secret value - the agent drops a workload report
field that contains one, and no other column can hold one; THREAT_MODEL RE-I6).

Query: ``campaign`` (one campaign's records) and ``since`` (records of attempts
created at or after this ISO-8601 time, with an offset). The body is the
Parquet file of :mod:`meshforge.controller.telemetry.export`, built off the
event loop and streamed in chunks; the same query over the same records gives
the same bytes (D222). More records than ``[controller.telemetry]
export_max_rows`` -> 413 ``invalid_input`` (narrow the query). Database down ->
503 ``network_error`` (``Retry-After``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.errors import ApiError

__all__ = [
    "PARQUET_MEDIA_TYPE",
    "TELEMETRY_STATE_KEY",
    "TelemetryExport",
    "telemetry_router",
]

log = get_logger(__name__)

TELEMETRY_STATE_KEY: Final = "meshforge_telemetry"
PARQUET_MEDIA_TYPE: Final = "application/vnd.apache.parquet"
CHUNK_BYTES: Final = 256 * 1024
_CAMPAIGN: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")

telemetry_router = APIRouter(prefix="/v1", tags=["telemetry"])


@dataclass(frozen=True, slots=True)
class TelemetryExport:
    """What the route needs: the database and the size cap."""

    engine: AsyncEngine
    max_rows: int


def get_export(request: Request) -> TelemetryExport:
    found: TelemetryExport | None = getattr(
        request.app.state, TELEMETRY_STATE_KEY, None
    )
    if found is None:
        raise ApiError(FailureType.CONFIG_ERROR, "telemetry export is not configured")
    return found


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), CHUNK_BYTES):
        yield data[start : start + CHUNK_BYTES]


@telemetry_router.get(
    "/telemetry/export",
    response_class=StreamingResponse,
    summary="The telemetry dataset (run records) as one Parquet file - admin only",
    responses={200: {"content": {PARQUET_MEDIA_TYPE: {}}}},
)
async def export_telemetry(
    admin: AdminUser,
    export: Annotated[TelemetryExport, Depends(get_export)],
    campaign: Annotated[
        str | None, Query(max_length=64, description="Only this campaign's records.")
    ] = None,
    since: Annotated[
        dt.datetime | None,
        Query(description="Only attempts created at or after this time (ISO 8601)."),
    ] = None,
) -> StreamingResponse:
    from meshforge.controller.telemetry.export import fetch_records, parquet_bytes
    from meshforge.controller.telemetry.recorder import TelemetryStoreError

    if campaign is not None and not _CAMPAIGN.fullmatch(campaign):
        raise ApiError.invalid("campaign is not a campaign id")
    if since is not None and since.tzinfo is None:
        raise ApiError.invalid("since needs a UTC offset (e.g. 2026-10-17T00:00:00Z)")
    try:
        records = await fetch_records(
            export.engine,
            campaign_id=campaign,
            since=since,
            limit=export.max_rows + 1,
        )
    except TelemetryStoreError:
        raise ApiError(
            FailureType.NETWORK_ERROR,
            "the database is unavailable; retry",
            headers={"Retry-After": "2"},
        ) from None
    if len(records) > export.max_rows:
        raise ApiError(
            FailureType.INVALID_INPUT,
            f"more than {export.max_rows} records match; narrow the export "
            "(campaign / since)",
            http_status=413,
        )
    data = await asyncio.to_thread(parquet_bytes, records)
    log.info(
        "controller.telemetry.exported",
        rows=len(records),
        bytes=len(data),
        campaign_id=campaign,
        user_id=admin.user_id,
    )
    name = f"meshforge-telemetry{'-' + campaign if campaign else ''}.parquet"
    return StreamingResponse(
        _chunks(data),
        media_type=PARQUET_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
            "X-MeshForge-Telemetry-Rows": str(len(records)),
        },
    )
