"""``GET /v1/telemetry/export`` - the telemetry dataset as Parquet (V0.9 G1, D211/D222).

**Admin only** (403 for a user): the records name every user's jobs, nodes and
benchmark parameters (never a secret value - the agent drops a workload report
field that contains one, and no other column can hold one; THREAT_MODEL RE-I6).

Query: ``campaign`` (one campaign's records) and ``since`` (records of attempts
created at or after this ISO-8601 time, with an offset); V0.10 G8 ``table``:
``runs`` (default) or ``chunks`` (the chunk records - ``chunks.parquet``,
D271). The body is the
Parquet file of :mod:`meshforge.controller.telemetry.export`, **streamed**
(G4 item 0.4, D233.4): the matching records are loaded first (so the size cap
and a database failure are still answered with a status), then each row group
is encoded off the event loop and sent as it is ready, in slices of at most
256 KiB - chunked, **no** ``Content-Length``; the whole file is never in
memory. The same query over the same records gives the same bytes (D222).
More records than ``[controller.telemetry] export_max_rows`` -> 413
``invalid_input`` (narrow the query). Database down -> 503 ``network_error``
(``Retry-After``). A failure while encoding (after the 200 is sent) aborts the
response: the client gets a truncated body (no Parquet footer), never a file
that looks complete.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from typing import Annotated, Final, Literal

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


async def _stream(
    pieces: Generator[bytes, None, None],
    *,
    rows: int,
    campaign: str | None,
    user_id: str | None,
) -> AsyncIterator[bytes]:
    """Each row group's bytes as it is encoded (in a worker thread), cut into
    :data:`CHUNK_BYTES` slices; logs the export once it is complete.

    A client that hangs up closes this generator (``GeneratorExit`` /
    cancellation - not an ``Exception``, so not ``export_aborted``); the
    ``finally`` then closes ``pieces`` at once, so its Parquet writer is shut
    deterministically instead of whenever the garbage collector runs."""
    sent = 0
    try:
        while True:
            piece = await asyncio.to_thread(next, pieces, None)
            if piece is None:
                break
            for start in range(0, len(piece), CHUNK_BYTES):
                yield piece[start : start + CHUNK_BYTES]
            sent += len(piece)
    except Exception as exc:
        log.exception(
            "controller.telemetry.export_aborted",
            rows=rows,
            bytes_sent=sent,
            campaign_id=campaign,
            user_id=user_id,
            error_type=type(exc).__name__,
        )
        raise
    finally:
        _close(pieces)
    log.info(
        "controller.telemetry.exported",
        rows=rows,
        bytes=sent,
        campaign_id=campaign,
        user_id=user_id,
    )


def _close(pieces: Generator[bytes, None, None]) -> None:
    try:
        pieces.close()
    except ValueError:
        # cancelled while a worker thread is still inside next(pieces): the
        # generator is executing and cannot be closed from here; it finishes
        # its row group in that thread and is closed when it is collected
        log.debug("controller.telemetry.export_close_deferred")


@telemetry_router.get(
    "/telemetry/export",
    response_class=StreamingResponse,
    summary="The telemetry dataset (run records) as one Parquet file - admin only",
    responses={
        200: {
            "content": {PARQUET_MEDIA_TYPE: {}, "application/octet-stream": {}},
            "description": "The Parquet file (telemetry schema v3, docs/TELEMETRY.md)",
        }
    },
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
    table: Annotated[
        Literal["runs", "chunks"],
        Query(
            description="runs = the run records; chunks = the chunk records "
            "(chunks.parquet, V0.10 G8: a campaign's = its jobs' chunks; since = "
            "leases granted at or after)."
        ),
    ] = "runs",
) -> StreamingResponse:
    from meshforge.controller.telemetry.export import (
        fetch_chunk_records,
        fetch_records,
        iter_chunk_parquet,
        iter_parquet,
    )
    from meshforge.controller.telemetry.recorder import TelemetryStoreError
    from meshforge.controller.telemetry.schema import SCHEMA_VERSION

    if campaign is not None and not _CAMPAIGN.fullmatch(campaign):
        raise ApiError.invalid("campaign is not a campaign id")
    if since is not None and since.tzinfo is None:
        raise ApiError.invalid("since needs a UTC offset (e.g. 2026-10-17T00:00:00Z)")
    fetch = fetch_chunk_records if table == "chunks" else fetch_records
    try:
        records = await fetch(
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
    stem = "meshforge-telemetry" if table == "runs" else "meshforge-chunks"
    name = f"{stem}{'-' + campaign if campaign else ''}.parquet"
    rows = len(records)
    pieces = (
        iter_chunk_parquet(records, sort_in_place=True)
        if table == "chunks"
        else iter_parquet(records, sort_in_place=True)
    )
    return StreamingResponse(
        _stream(pieces, rows=rows, campaign=campaign, user_id=admin.user_id),
        media_type=PARQUET_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-MeshForge-Telemetry-Rows": str(rows),
            "X-MeshForge-Telemetry-Schema": str(SCHEMA_VERSION),
        },
    )
