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

**Memory (G3 item 0.7, D230.1; G4 items 0.4/0.12, D233.4/D240).** The records
are **loaded** from the database (:func:`fetch_records`: every matching record,
~2.5 KB each as dicts, more as ORM rows, at most :data:`MAX_EXPORT_ROWS` /
``[controller.telemetry] export_max_rows``), but the **file is streamed**:
:func:`iter_parquet` builds Arrow data and encodes Parquet **one row group at a
time** and yields each row group's bytes as soon as it is written (the footer
last), so neither the whole Arrow table nor the whole file is ever held - only
one row group (:data:`ROW_GROUP_SIZE` rows) and its encoded bytes. The API
streams those bytes chunked (no ``Content-Length``). The export order is a
sort of the loaded records: :func:`iter_parquet` sorts a list **in place**
when asked (``sort_in_place=True``, the route's own list) instead of copying it;
otherwise it sorts a copy of the references (8 bytes per row, the dicts are
shared). :func:`parquet_bytes` (tests, scripts) still returns the whole file.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from typing import IO, Any, Final

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.telemetry import ChunkRecord, RunRecord
from meshforge.controller.telemetry.parquet_options import (
    ROW_GROUP_SIZE,
    WRITER_OPTIONS,
)
from meshforge.controller.telemetry.record import canonical_json
from meshforge.controller.telemetry.recorder import TelemetryStoreError
from meshforge.controller.telemetry.schema import (
    CHUNK_COLUMN_NAMES,
    CHUNK_COLUMNS,
    COLUMN_NAMES,
    COLUMNS,
    SCHEMA_VERSION,
    Column,
    Kind,
)

__all__ = [
    "ARROW_TYPES",
    "MAX_EXPORT_ROWS",
    "ROW_GROUP_SIZE",
    "WRITER_OPTIONS",
    "Chunks",
    "arrow_schema",
    "chunk_parquet_bytes",
    "chunk_sort_key",
    "chunks_table",
    "fetch_chunk_records",
    "fetch_records",
    "iter_chunk_parquet",
    "iter_parquet",
    "parquet_bytes",
    "records_table",
    "sort_key",
    "write_parquet",
    "write_records",
]

MAX_EXPORT_ROWS: Final = 100_000
"""Default cap of one export (``[controller.telemetry] export_max_rows``): a
campaign is thousands of records, and the records are loaded before the file is
streamed (module docstring) - ~0.25 GB of record dicts at the cap."""

# WRITER_OPTIONS / ROW_GROUP_SIZE: pinned in .parquet_options (V0.15 G1 - the
# pure ML dataset encoder shares them without importing this module).

ARROW_TYPES: Final[dict[Kind, pa.DataType]] = {  # public: labels_export reuses it
    Kind.TEXT: pa.string(),
    Kind.INT64: pa.int64(),
    Kind.FLOAT64: pa.float64(),
    Kind.BOOL: pa.bool_(),
    Kind.JSON: pa.string(),
    Kind.TIMESTAMP: pa.timestamp("us", tz="UTC"),
}
SCHEMA_METADATA_KEY: Final = b"meshforge.telemetry.schema_version"


def arrow_schema(columns: tuple[Column, ...] = COLUMNS) -> pa.Schema:
    """The export's Arrow schema, derived from :data:`~.schema.COLUMNS` (or,
    V0.10 G8, :data:`~.schema.CHUNK_COLUMNS` for ``chunks.parquet``)."""
    fields = [
        pa.field(c.name, ARROW_TYPES[c.kind], nullable=c.nullable) for c in columns
    ]
    return pa.schema(
        fields, metadata={SCHEMA_METADATA_KEY: str(SCHEMA_VERSION).encode()}
    )


def chunk_sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """``chunks.parquet`` order: (job_id, chunk_index, lease_seq) - the
    primary key, so a total order."""
    return (
        str(record.get("job_id")),
        int(record.get("chunk_index") or 0),
        int(record.get("lease_seq") or 0),
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
    """The records as one Arrow table, in export order (see the module
    docstring). Pure."""
    return _rows_table(sorted(records, key=sort_key), arrow_schema())


def chunks_table(records: Iterable[Mapping[str, Any]]) -> pa.Table:
    """V0.10 G8: chunk records as one Arrow table, in export order. Pure."""
    return _rows_table(
        sorted(records, key=chunk_sort_key),
        arrow_schema(CHUNK_COLUMNS),
        CHUNK_COLUMNS,
    )


def _rows_table(
    rows: Sequence[Mapping[str, Any]],
    schema: pa.Schema,
    declared: tuple[Column, ...] = COLUMNS,
) -> pa.Table:
    """``rows`` (already in export order) as an Arrow table."""
    columns = [
        pa.array([_cell(c, r) for r in rows], type=ARROW_TYPES[c.kind])
        for c in declared
    ]
    return pa.Table.from_arrays(columns, schema=schema)


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
    """Write a ready ``table`` with the pinned options (:data:`WRITER_OPTIONS`).
    Same bytes as :func:`write_records` of the same rows."""
    pq.write_table(table, sink, row_group_size=ROW_GROUP_SIZE, **WRITER_OPTIONS)


class Chunks:
    """A write-only binary sink that hands out what was written so far
    (:meth:`take`); ``tell`` counts every byte ever written (pyarrow asks)."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._written = 0
        self.closed = False

    def write(self, data: bytes | bytearray | memoryview) -> int:
        chunk = bytes(data)
        self._parts.append(chunk)
        self._written += len(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self._written

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readable(self) -> bool:
        return False

    def take(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        return data


def _ordered(
    records: Iterable[Mapping[str, Any]],
    *,
    in_place: bool,
    key: Callable[[Mapping[str, Any]], tuple[Any, ...]] = sort_key,
) -> Sequence[Mapping[str, Any]]:
    if in_place and isinstance(records, list):
        records.sort(key=key)
        return records
    return sorted(records, key=key)


def iter_parquet(
    records: Iterable[Mapping[str, Any]], *, sort_in_place: bool = False
) -> Generator[bytes, None, None]:
    """The export of ``records`` as consecutive pieces of one Parquet file:
    one piece per row group as soon as it is encoded, the footer last (module
    docstring). Joined, the pieces equal :func:`write_parquet` of
    :func:`records_table` - the row groups and options are the same.
    ``sort_in_place`` sorts a ``list`` argument in place (it is reordered)
    instead of sorting a copy."""
    rows = _ordered(records, in_place=sort_in_place)
    return _iter_rows(rows, COLUMNS)


def iter_chunk_parquet(
    records: Iterable[Mapping[str, Any]], *, sort_in_place: bool = False
) -> Generator[bytes, None, None]:
    """V0.10 G8 (D271): ``chunks.parquet`` - :func:`iter_parquet`'s rules
    (pinned writer options, streamed row groups, no export-time value; D222)
    over chunk records in :func:`chunk_sort_key` order."""
    rows = _ordered(records, in_place=sort_in_place, key=chunk_sort_key)
    return _iter_rows(rows, CHUNK_COLUMNS)


def chunk_parquet_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    """The whole ``chunks.parquet`` as bytes (tests, scripts)."""
    return b"".join(iter_chunk_parquet(records))


def _iter_rows(
    rows: Sequence[Mapping[str, Any]], declared: tuple[Column, ...]
) -> Generator[bytes, None, None]:
    schema = arrow_schema(declared)
    sink = Chunks()
    writer = pq.ParquetWriter(sink, schema, **WRITER_OPTIONS)
    try:
        if not rows:
            writer.write_table(
                _rows_table([], schema, declared), row_group_size=ROW_GROUP_SIZE
            )
        for start in range(0, len(rows), ROW_GROUP_SIZE):
            group = _rows_table(rows[start : start + ROW_GROUP_SIZE], schema, declared)
            writer.write_table(group, row_group_size=ROW_GROUP_SIZE)
            del group
            piece = sink.take()
            if piece:
                yield piece
    finally:
        writer.close()
    tail = sink.take()
    if tail:
        yield tail


def write_records(records: Iterable[Mapping[str, Any]], sink: IO[bytes]) -> int:
    """Write the export of ``records`` to ``sink`` (:func:`iter_parquet`, one
    row group of Arrow data at a time) with the pinned options; returns the row
    count. The bytes equal :func:`write_parquet` of :func:`records_table`."""
    rows = list(records)
    for piece in iter_parquet(rows, sort_in_place=True):
        sink.write(piece)
    return len(rows)


def parquet_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    """The whole export as bytes, in memory (tests, scripts; the API streams
    :func:`iter_parquet`)."""
    return b"".join(iter_parquet(records))


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


async def fetch_chunk_records(
    engine: AsyncEngine,
    *,
    campaign_id: str | None = None,
    since: dt.datetime | None = None,
    limit: int = MAX_EXPORT_ROWS,
) -> list[dict[str, Any]]:
    """V0.10 G8: the stored chunk records - of one campaign's jobs (the jobs
    its run records name), or of leases granted at or after ``since`` - as
    column -> value mappings of :data:`~.schema.CHUNK_COLUMNS` (never the
    stderr tail), in export order. Raises :class:`~.recorder.TelemetryStoreError`."""
    columns = [getattr(ChunkRecord, name) for name in CHUNK_COLUMN_NAMES]
    query = select(*columns)
    if campaign_id is not None:
        jobs = (
            select(RunRecord.job_id)
            .where(RunRecord.campaign_id == campaign_id)
            .distinct()
            .scalar_subquery()
        )
        query = query.where(ChunkRecord.job_id.in_(jobs))
    if since is not None:
        query = query.where(ChunkRecord.leased_at >= since)
    query = query.order_by(
        ChunkRecord.job_id, ChunkRecord.chunk_index, ChunkRecord.lease_seq
    ).limit(limit)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            rows = (await db.execute(query)).all()
    except SQLAlchemyError as exc:
        raise TelemetryStoreError(
            "the database refused the telemetry export",
            details={"cause": type(exc).__name__},
        ) from exc
    return [dict(zip(CHUNK_COLUMN_NAMES, row, strict=True)) for row in rows]
