"""V0.9 G5 lane A item 5: the labels table and its Parquet file.

``labels_version`` 1 is pinned by a column fingerprint (a shape change without a
version bump fails, like the run-record schema); the file is byte-identical
across exports, input orders and row-group boundaries (D222), carries only the
labels version as custom metadata, and reads back to the same rows.
"""

from __future__ import annotations

import io
import json
import random

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from test_v09_g5a_labels import CAMP, runs, series  # same folder

from meshforge.controller.telemetry import labels_export as lx
from meshforge.controller.telemetry.export import WRITER_OPTIONS
from meshforge.controller.telemetry.labels import (
    LABELS_VERSION,
    METRICS,
    CampaignLabels,
    build_labels,
)
from meshforge.controller.telemetry.schema import Kind

pytestmark = pytest.mark.unit


def _labels(groups: int = 3) -> CampaignLabels:
    plan, rows = [], []
    for g in range(groups):
        gid = f"g{g:03d}"
        plan += runs("a", group=gid) + runs("b", group=gid) + runs("c", group=gid)
        rows += series(
            "a", [100 + g, 101 + g, 102 + g], group=gid, bytes_in=1, bytes_out=2
        )
        rows += series("b", [200, 250, 300 + g], group=gid)
        rows += series("c", [1, 2] if g % 2 else [500, 501, 502], group=gid)
    return build_labels(CAMP, plan, rows)


def test_labels_version_one_is_pinned_by_its_fingerprint() -> None:
    assert LABELS_VERSION == 1
    assert set(lx.LABEL_FINGERPRINTS) == {1}
    assert lx.label_fingerprint() == lx.LABEL_FINGERPRINTS[LABELS_VERSION]


def test_a_column_change_changes_the_fingerprint() -> None:
    cols = lx.LABEL_COLUMNS
    dropped = cols[:-1]
    retyped = (*cols[:-1], cols[-1].__class__(cols[-1].name, Kind.TEXT, True, ""))
    renamed = (cols[0].__class__("x", cols[0].kind, False, ""), *cols[1:])
    for changed in (dropped, retyped, renamed):
        assert lx.label_fingerprint(changed) != lx.LABEL_FINGERPRINTS[1]
    # wording of a description is not a shape change
    reworded = (cols[0].__class__(cols[0].name, cols[0].kind, False, "new"), *cols[1:])
    assert lx.label_fingerprint(reworded) == lx.LABEL_FINGERPRINTS[1]


def test_columns_cover_every_metric_and_rows_match_them() -> None:
    names = set(lx.LABEL_COLUMN_NAMES)
    for metric in METRICS:
        assert {f"{metric}_n", f"{metric}_median", f"{metric}_cv"} <= names
        assert {f"{metric}_ci_low", f"{metric}_ci_high", f"{metric}_outliers"} <= names
    rows = lx.labels_table_rows(_labels())
    assert all(set(r) == names for r in rows)
    assert len(lx.LABEL_COLUMN_NAMES) == len(names)


def test_rows_are_sorted_by_group_then_config() -> None:
    rows = lx.labels_table_rows(_labels(4))
    keys = [(r["group_id"], r["config_id"]) for r in rows]
    assert keys == sorted(keys) and len(keys) == 12
    first = rows[0]
    assert first["label"] == "best" and first["is_best"] is True
    assert rows[1]["is_best"] is False


def test_tie_rows_mark_the_tied_set() -> None:
    plan = runs("a") + runs("b")
    labels = build_labels(CAMP, plan, series("a", [1, 5, 9]) + series("b", [2, 6, 10]))
    rows = lx.labels_table_rows(labels)
    assert [r["in_tie"] for r in rows] == [True, True]
    assert rows[0]["tied_config_ids"] == ["a", "b"]
    assert rows[0]["total_ms_n"] == 3 and rows[0]["bytes_n"] == 0
    assert rows[0]["bytes_median"] is None


@pytest.mark.parametrize("groups", [0, 1, 2, 5])
def test_bytes_are_identical_across_exports_orders_and_row_groups(
    monkeypatch: pytest.MonkeyPatch, groups: int
) -> None:
    labels = _labels(groups)
    first = lx.labels_parquet_bytes(labels)
    assert lx.labels_parquet_bytes(labels) == first  # twice (D222)
    # the same measurements recorded again (new attempt ids, times) -> same bytes
    again = _labels(groups)
    assert lx.labels_parquet_bytes(again) == first
    monkeypatch.setattr(lx, "ROW_GROUP_SIZE", 2)
    pieces = list(lx.iter_labels_parquet(lx.labels_table_rows(labels)))
    meta = pq.read_metadata(io.BytesIO(b"".join(pieces)))
    assert meta.num_rows == 3 * groups
    assert meta.num_row_groups == max(1, -(-3 * groups // 2))
    if groups:  # one piece per row group, then the footer
        assert len(pieces) == -(-3 * groups // 2) + 1
    assert b"".join(pieces) != first or groups <= 0  # other layout, other bytes


def test_shuffled_inputs_give_the_same_file() -> None:
    plan = runs("a") + runs("b") + runs("c")
    rows = (
        series("a", [100, 101, 102]) + series("b", [5, 500, 9]) + series("c", [7, 8, 9])
    )
    first = lx.labels_parquet_bytes(build_labels(CAMP, plan, rows))
    rng = random.Random(3)  # noqa: S311 - seeded shuffle
    for _ in range(4):
        p, r = plan[:], rows[:]
        rng.shuffle(p)
        rng.shuffle(r)
        assert lx.labels_parquet_bytes(build_labels(CAMP, p, r)) == first


def test_the_file_reads_back_to_the_rows_with_only_the_version_metadata() -> None:
    labels = _labels(3)
    data = lx.labels_parquet_bytes(labels)
    table = pq.read_table(io.BytesIO(data))
    assert table.column_names == list(lx.LABEL_COLUMN_NAMES)
    custom = {
        k: v for k, v in (table.schema.metadata or {}).items() if k != b"ARROW:schema"
    }
    assert custom == {lx.LABELS_METADATA_KEY: b"1"}
    back = table.to_pylist()
    expected = lx.labels_table_rows(labels)
    for got, want in zip(back, expected, strict=True):
        for key, value in want.items():
            if key in ("params", "tied_config_ids"):
                assert json.loads(got[key]) == value
            else:
                assert got[key] == value, key
    meta = pq.read_metadata(io.BytesIO(data))
    assert meta.format_version == WRITER_OPTIONS["version"]
