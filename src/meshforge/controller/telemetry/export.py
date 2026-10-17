"""The dataset: run records -> one Parquet file (V0.9 G1, D211/D222, ADR-0018).

:func:`records_table` is a **pure function** from records (column -> value
mappings, as :func:`fetch_records` reads them) to an Arrow table; nothing writes
Parquet during execution. The export is **reproducible** (D222): the same
records give the same bytes -

* rows sorted by (campaign_id, config_id, repetition, attempt_id), nulls first;
* columns in :data:`~.schema.COLUMNS` order with fixed Arrow types (``json``
  columns as canonical JSON text: sorted keys, compact);
* no export-time value anywhere in the file (no "exported at", no row count
  that depends on the call); the file metadata carries only the schema version;
* the writer's options pinned in :data:`WRITER_OPTIONS` (format version,
  compression, dictionary, statistics, row-group size) - and one pyarrow
  version (``created_by`` names it; ``uv.lock`` pins it).

A test writes the same records twice and compares the bytes, and reads the file
back and compares it with the records.
"""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Iterable, Mapping, Sequence
from typing import IO, Any, Final

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.record import canonical_json
from meshforge.controller.telemetry.recorder import TelemetryStoreError
from meshforge.controller.telemetry.schema import (
    COLUMN_NAMES,
    COLUMNS,
    SCHEMA_VERSION,
    Column,
    Kind,
)

__all__ = [
    "MAX_EXPORT_ROWS",
    "WRITER_OPTIONS",
    "arrow_schema",
    "fetch_records",
    "parquet_bytes",
    "records_table",
    "sort_key",
    "write_parquet",
]

MAX_EXPORT_ROWS: Final = 1_000_000
"""One export holds at most this many records (a campaign is thousands)."""

WRITER_OPTIONS: Final[dict[str, Any]] = {
    "version": "2.6",
    "compression": "zstd",
    "compression_level": 3,
    "use_dictionary": False,
    "write_statistics": True,
    "data_page_version": "1.0",
    "store_schema": True,
}
"""Pinned Parquet writer options (D222): changing one changes the bytes."""
ROW_GROUP_SIZE: Final = 65_536

_ARROW_TYPES: Final[dict[Kind, pa.DataType]] = {
    Kind.TEXT: pa.string(),
    Kind.INT64: pa.int64(),
    Kind.FLOAT64: pa.float64(),
    Kind.BOOL: pa.bool_(),
    Kind.JSON: pa.string(),
    Kind.TIMESTAMP: pa.timestamp("us", tz="UTC"),
}
SCHEMA_METADATA_KEY: Final = b"meshforge.telemetry.schema_version"


def arrow_schema() -> pa.Schema:
    """The export's Arrow schema, derived from :data:`~.schema.COLUMNS`."""
    fields = [
        pa.field(c.name, _ARROW_TYPES[c.kind], nullable=c.nullable) for c in COLUMNS
    ]
    return pa.schema(
        fields, metadata={SCHEMA_METADATA_KEY: str(SCHEMA_VERSION).encode()}
    )


def sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """(campaign_id, config_id, repetition, attempt_id), nulls first."""

    def part(value: Any) -> tuple[int, Any]:
        return (0, "") if value is None else (1, value)

    return (
        part(record.get("campaign_id")),
        part(record.get("config_id")),
        part(record.get("repetition")),
        part(record.get("attempt_id")),
    )


def records_table(records: Iterable[Mapping[str, Any]]) -> pa.Table:
    """The records as an Arrow table (see the module docstring). Pure."""
    rows = sorted(records, key=sort_key)
    columns = [
        pa.array([_cell(c, r) for r in rows], type=_ARROW_TYPES[c.kind])
        for c in COLUMNS
    ]
    return pa.Table.from_arrays(columns, schema=arrow_schema())


def _cell(column: Column, record: Mapping[str, Any]) -> Any:
    value = record.get(column.name)
    if value is None:
        if not column.nullable:
            raise ValueError(f"run record column {column.name} is null")
        return None
    if column.kind is Kind.JSON:
        return canonical_json(value)
    if column.kind is Kind.TIMESTAMP:
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            raise ValueError(f"{column.name} must be an aware datetime")
        return value.astimezone(dt.UTC)
    return value


def write_parquet(table: pa.Table, sink: IO[bytes]) -> None:
    """Write ``table`` with the pinned options (:data:`WRITER_OPTIONS`)."""
    pq.write_table(table, sink, row_group_size=ROW_GROUP_SIZE, **WRITER_OPTIONS)


def parquet_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    """The whole export in memory (records -> table -> Parquet)."""
    buffer = io.BytesIO()
    write_parquet(records_table(records), buffer)
    return buffer.getvalue()


async def fetch_records(
    engine: AsyncEngine,
    *,
    campaign_id: str | None = None,
    since: dt.datetime | None = None,
    limit: int = MAX_EXPORT_ROWS,
) -> list[dict[str, Any]]:
    """The stored records (optionally one campaign's, or those whose attempt
    was created at or after ``since``), as column -> value mappings, in export
    order. Raises :class:`~.recorder.TelemetryStoreError`."""
    query = select(RunRecord)
    if campaign_id is not None:
        query = query.where(RunRecord.campaign_id == campaign_id)
    if since is not None:
        query = query.where(RunRecord.attempt_created_at >= since)
    query = query.order_by(
        RunRecord.campaign_id.asc().nulls_first(),
        RunRecord.config_id.asc().nulls_first(),
        RunRecord.repetition.asc().nulls_first(),
        RunRecord.attempt_id,
    ).limit(limit)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            rows: Sequence[RunRecord] = list(await db.scalars(query))
    except SQLAlchemyError as exc:
        raise TelemetryStoreError(
            "the database refused the telemetry export",
            details={"cause": type(exc).__name__},
        ) from exc
    return [{name: getattr(row, name) for name in COLUMN_NAMES} for row in rows]
