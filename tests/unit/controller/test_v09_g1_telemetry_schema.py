"""V0.9 G1 item 4 (D212): the telemetry schema gate.

``controller/telemetry/schema.py`` declares the run-record columns and the
version. This test fails when the ``run_record`` model, the export's Arrow
schema or the builder's output differ from that declaration, or when the
columns change without a version bump (the fingerprint of every published
version is frozen in ``SCHEMA_FINGERPRINTS``).
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String
from sqlalchemy.dialects.postgresql import JSONB

from meshforge.controller.store.models import telemetry as model_module
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry import schema
from meshforge.controller.telemetry.schema import (
    COLUMNS,
    SCHEMA_FINGERPRINTS,
    SCHEMA_VERSION,
    Kind,
    fingerprint,
)

pytestmark = pytest.mark.unit

_SQL_KIND = {
    String: Kind.TEXT,
    BigInteger: Kind.INT64,
    Float: Kind.FLOAT64,
    Boolean: Kind.BOOL,
    JSONB: Kind.JSON,
    DateTime: Kind.TIMESTAMP,
}


def _kind(sql_type: object) -> Kind:
    for cls, kind in _SQL_KIND.items():
        if isinstance(sql_type, cls):
            return kind
    raise AssertionError(f"unmapped column type {sql_type!r}")


def test_the_model_matches_the_declared_columns_exactly() -> None:
    table = RunRecord.__table__
    declared = [(c.name, c.kind, c.nullable) for c in COLUMNS]
    actual = [(c.name, _kind(c.type), bool(c.nullable)) for c in table.columns]
    assert actual == declared


def test_the_export_schema_matches_the_declared_columns_exactly() -> None:
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    import pyarrow as pa

    from meshforge.controller.telemetry.export import arrow_schema

    arrow = arrow_schema()
    assert arrow.names == [c.name for c in COLUMNS]
    expected = {
        Kind.TEXT: pa.string(),
        Kind.INT64: pa.int64(),
        Kind.FLOAT64: pa.float64(),
        Kind.BOOL: pa.bool_(),
        Kind.JSON: pa.string(),
        Kind.TIMESTAMP: pa.timestamp("us", tz="UTC"),
    }
    for column, field in zip(COLUMNS, arrow, strict=True):
        assert field.type == expected[column.kind], column.name
        assert field.nullable == column.nullable, column.name
    assert arrow.metadata == {
        b"meshforge.telemetry.schema_version": str(SCHEMA_VERSION).encode()
    }


def test_the_builder_produces_exactly_the_declared_columns() -> None:
    import datetime as dt

    from meshforge.controller.telemetry.record import RecordSource, build_record

    record = build_record(
        RecordSource(
            attempt_id="att_1",
            job_id="job_1",
            attempt_no=1,
            node_id="node_1",
            state="done",
            created_at=dt.datetime(2026, 10, 17, tzinfo=dt.UTC),
        )
    )
    assert set(record) == {c.name for c in COLUMNS} - {"recorded_at"}


def test_columns_cannot_change_without_a_version_bump() -> None:
    """Changing, adding, removing or reordering a column changes the
    fingerprint; the published version's fingerprint is frozen, so the
    change needs SCHEMA_VERSION + 1 and a new SCHEMA_FINGERPRINTS entry."""
    assert SCHEMA_VERSION in SCHEMA_FINGERPRINTS, "record the new version"
    assert fingerprint() == SCHEMA_FINGERPRINTS[SCHEMA_VERSION], (
        "the run-record columns changed: bump telemetry.schema.SCHEMA_VERSION "
        "and add its fingerprint (never edit a published one)"
    )
    assert max(SCHEMA_FINGERPRINTS) == SCHEMA_VERSION
    assert len(set(SCHEMA_FINGERPRINTS.values())) == len(SCHEMA_FINGERPRINTS)


def test_the_fingerprint_reacts_to_every_kind_of_change() -> None:
    base = fingerprint()
    renamed = (schema.Column("x", Kind.TEXT, True, ""), *COLUMNS[1:])
    retyped = (schema.Column(COLUMNS[0].name, Kind.TEXT, False, ""), *COLUMNS[1:])
    nullable = (
        schema.Column(COLUMNS[0].name, COLUMNS[0].kind, True, ""),
        *COLUMNS[1:],
    )
    reordered = (COLUMNS[1], COLUMNS[0], *COLUMNS[2:])
    redoc = (
        schema.Column(COLUMNS[0].name, COLUMNS[0].kind, False, "other words"),
        *COLUMNS[1:],
    )
    for changed in (renamed, retyped, nullable, reordered, COLUMNS[:-1]):
        assert fingerprint(changed) != base
    assert fingerprint(redoc) == base  # a description is not the schema


def test_the_model_checks_use_the_schemas_value_lists() -> None:
    assert model_module._FAILURE_CLASSES == schema.FAILURE_CLASSES
    assert model_module._STRATEGIES == schema.STRATEGIES


def test_column_names_are_unique_and_documented() -> None:
    names = [c.name for c in COLUMNS]
    assert len(names) == len(set(names))
    assert all(c.doc.strip() for c in COLUMNS)
    required = {
        "schema_version",
        "campaign_id",
        "failure_class",
        "attempt_id",
        "clock_offset_ms",
        "compute_ns",
    }
    assert required <= set(names)  # checklist 29


def test_the_schema_doc_table_lists_every_column() -> None:
    """docs/TELEMETRY.md is the human copy of the schema (item 7)."""
    from pathlib import Path

    doc = Path(__file__).resolve().parents[3] / "docs" / "TELEMETRY.md"
    text = doc.read_text(encoding="utf-8")
    missing = [c.name for c in COLUMNS if f"`{c.name}`" not in text]
    assert missing == []
    assert f"schema version {SCHEMA_VERSION}" in text.lower()
