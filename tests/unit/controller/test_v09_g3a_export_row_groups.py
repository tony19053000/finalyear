"""V0.9 G3 lane A item 0.7 (D230.1): the export writes Parquet one row group at a
time, and stays byte-identical (D222) across exports and across row-group
boundaries; the default cap is lower and documented as buffered."""

from __future__ import annotations

import io
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from test_v09_g1_run_record import _records  # the G1 record factory, same folder

from meshforge.controller.settings import TelemetrySettings
from meshforge.controller.telemetry import export

pytestmark = pytest.mark.unit


def _old_way(records: list[dict[str, Any]]) -> bytes:
    """The G1 writer: the whole table, then ``pq.write_table``."""
    buffer = io.BytesIO()
    export.write_parquet(export.records_table(records), buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 6, 7, 10])
def test_row_group_boundaries_change_no_byte(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    monkeypatch.setattr(export, "ROW_GROUP_SIZE", 3)
    records = _records(count)
    shuffled = list(reversed(records[1::2])) + records[::2]  # another order
    first = export.parquet_bytes(records)
    assert export.parquet_bytes(shuffled) == first  # any input order
    assert export.parquet_bytes(records) == first  # twice
    assert _old_way(records) == first  # same row groups as the one-table writer
    meta = pq.read_metadata(io.BytesIO(first))
    assert meta.num_rows == count
    assert meta.num_row_groups == max(1, -(-count // 3))
    back = pq.read_table(io.BytesIO(first)).column("attempt_id").to_pylist()
    expected = [r["attempt_id"] for r in sorted(records, key=export.sort_key)]
    assert back == expected


def test_arrow_data_is_built_one_row_group_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export, "ROW_GROUP_SIZE", 4)
    sizes: list[int] = []
    real = export._rows_table

    def spy(rows: Any, schema: Any, *rest: Any) -> Any:
        sizes.append(len(rows))
        return real(rows, schema, *rest)

    monkeypatch.setattr(export, "_rows_table", spy)
    buffer = io.BytesIO()
    assert export.write_records(_records(10), buffer) == 10
    assert sizes == [4, 4, 2]


def test_the_default_row_group_layout_is_unchanged() -> None:
    """65 536 rows per group, as docs/TELEMETRY.md and D222 pin it."""
    assert export.ROW_GROUP_SIZE == 65_536
    meta = pq.read_metadata(io.BytesIO(export.parquet_bytes(_records(5))))
    assert meta.num_row_groups == 1


def test_the_default_cap_is_lower_and_the_same_in_settings() -> None:
    assert export.MAX_EXPORT_ROWS == 100_000
    assert TelemetrySettings().export_max_rows == export.MAX_EXPORT_ROWS


def test_the_docstrings_say_what_is_held() -> None:
    """G4 item 0.4 (D233.4) made the file streamed (the records are still
    loaded); the docstrings say exactly that."""
    assert export.__doc__ is not None
    assert "the **file is streamed**" in export.__doc__
    assert "are **loaded**" in export.__doc__
    assert export.parquet_bytes.__doc__ is not None
    assert "in memory" in export.parquet_bytes.__doc__
