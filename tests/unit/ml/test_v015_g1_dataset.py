"""V0.15 G1 (D333): the dataset - rows, counts, canonical Parquet, digest.

* Rows carry the ids, group (fingerprint; ``job:<id>`` without one), family,
  features (NaN preserved through Parquet) and targets; sorted by
  ``(finished_at, decision_id, attempt_id, term, node_id)``.
* A missing / unreadable replay skips the decision once and is counted.
* Same runs in any order -> the same bytes and digest; any change -> another
  digest; the file reads back to the rows; its only custom metadata are the
  two versions.
* The schema fingerprint is pinned per (dataset, feature) version.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import math
import random

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from mf_ml_fixtures import set_run, stored, whole_run

from meshforge.controller.ml import dataset as ds
from meshforge.controller.ml.schema import (
    DATASET_VERSION,
    FEATURE_VERSION,
    NUMERIC_FEATURES,
    SCHEMA_FINGERPRINTS,
    schema_fingerprint,
)
from meshforge.controller.ml.targets import RunInput, Skip

pytestmark = pytest.mark.unit

PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}
T0 = dt.datetime(2026, 10, 22, 9, 0, tzinfo=dt.UTC)


def _runs() -> list[RunInput]:
    mode1 = stored(pinned="node_001")
    local = stored()
    split = stored(forced_set=True)
    runs: list[RunInput] = []
    for i in range(6):
        s = mode1 if i % 2 else local
        runs.append(
            whole_run(
                s,
                phases={**PHASES, "run": 100_000 + 1000 * i},
                decision_id=i + 1,
                attempt_id=f"att_{i}",
                finished_at=T0 + dt.timedelta(minutes=i % 3),  # ties on time
                family="cpu.primes" if i < 4 else None,
                fingerprint=f"fp_{i % 3}" if i != 5 else None,
            )
        )
    runs.append(set_run(split, decision_id=50))
    runs.append(whole_run(mode1, phases=PHASES, decision_id=60, replay=False))
    broken = dict(mode1.replay, replay_version=99)
    runs.append(whole_run(mode1, phases=PHASES, decision_id=61, replay=broken))
    return runs


def test_rows_ids_groups_and_counts() -> None:
    data = ds.build(_runs())
    rows = data.rows
    assert [ds.sort_key(r) for r in rows] == sorted(ds.sort_key(r) for r in rows)
    assert list(rows[0]) == list(ds.COLUMN_NAMES)
    groups = {r["group"] for r in rows}
    assert {"fp_0", "fp_1", "fp_2", "fp_set", "job:job_a"} == groups  # 5: no fp
    assert {r["family"] for r in rows} == {"cpu.primes", None, "cpu.split"}
    for row in rows:
        assert row["y"] == math.log(row["actual_s"] / row["raw_s"])
        assert row["an_raw_s"] == row["raw_s"]
        assert row["finished_at"].tzinfo is dt.UTC
    counts = data.counts
    assert counts["runs"] == 9
    assert counts["runs_with_rows"] == 7
    assert counts["rows"] == len(rows)
    assert counts["skipped"]["missing_replay"] == 1
    assert counts["skipped"]["unreadable_replay"] == 1
    assert (
        counts["skipped"]["exact_by_construction"] == 3 + 1
    )  # 3 local 0-byte transfers + 1 lane
    assert set(counts["skipped"]) == {s.value for s in Skip}
    assert counts["rows_by_term"]["chunk_rate"] == 3
    assert counts["rows_by_term"]["combine"] == 1
    assert counts["rows_by_term"]["compute"] == 6
    assert sum(counts["rows_by_term"].values()) == len(rows)
    assert counts["rows_by_family"] == {
        "(none)": sum(1 for r in rows if r["family"] is None),
        "cpu.primes": sum(1 for r in rows if r["family"] == "cpu.primes"),
        "cpu.split": 9,
    }


def test_a_decision_without_replay_is_skipped_and_counted_never_imputed() -> None:
    s = stored(pinned="node_001")
    data = ds.build([whole_run(s, phases=PHASES, replay=False)])
    assert data.rows == ()
    assert data.counts["skipped"]["missing_replay"] == 1
    assert data.counts["runs"] == 1 and data.counts["runs_with_rows"] == 0


def test_same_rows_in_any_order_give_the_same_bytes_and_digest() -> None:
    runs = _runs()
    first = ds.build(runs)
    rng = random.Random(7)  # noqa: S311 - test order, not security
    for _ in range(5):
        shuffled = runs[:]
        rng.shuffle(shuffled)
        again = ds.build(shuffled)
        assert again.parquet == first.parquet
        assert again.digest == first.digest
    assert first.digest == "sha256:" + hashlib.sha256(first.parquet).hexdigest()


def test_any_change_moves_the_digest() -> None:
    runs = _runs()
    base = ds.build(runs).digest
    changed = runs[:]
    changed[0] = dataclasses.replace(
        runs[0],
        outcome=dataclasses.replace(runs[0].outcome, phases_ms={**PHASES, "run": 1}),
    )
    assert ds.build(changed).digest != base
    assert ds.build(runs[1:]).digest != base


def test_the_file_reads_back_to_the_rows_with_nan_kept() -> None:
    data = ds.build(_runs())
    table = pq.read_table(io.BytesIO(data.parquet))
    assert table.column_names == list(ds.COLUMN_NAMES)
    back = table.to_pylist()
    assert len(back) == len(data.rows)
    nan_seen = 0
    for got, want in zip(back, data.rows, strict=True):
        for name in ds.COLUMN_NAMES:
            a, b = got[name], want[name]
            if isinstance(b, float) and math.isnan(b):
                assert isinstance(a, float) and math.isnan(a), name  # NaN, not null
                nan_seen += 1
            else:
                assert a == b, name
    assert nan_seen > 0
    meta = table.schema.metadata
    assert meta == {
        ds.DATASET_METADATA_KEY: str(DATASET_VERSION).encode(),
        ds.FEATURE_METADATA_KEY: str(FEATURE_VERSION).encode(),
    }


def test_row_groups_do_not_change_the_rows_and_empty_is_a_valid_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = ds.build(_runs())
    monkeypatch.setattr(ds, "ROW_GROUP_SIZE", 3)
    small = ds.parquet_bytes(list(data.rows))

    def canon(blob: bytes) -> str:  # NaN != NaN: compare a text form
        return repr(pq.read_table(io.BytesIO(blob)).to_pylist())

    assert canon(small) == canon(data.parquet)
    assert pq.ParquetFile(io.BytesIO(small)).num_row_groups > 1
    empty = ds.build([])
    assert pq.read_table(io.BytesIO(empty.parquet)).num_rows == 0
    assert empty.counts["rows"] == 0
    assert empty.digest == ds.build([]).digest


def test_feature_columns_follow_the_schema_order() -> None:
    names = ds.COLUMN_NAMES
    start = names.index(NUMERIC_FEATURES[0])
    assert names[start : start + len(NUMERIC_FEATURES)] == NUMERIC_FEATURES
    assert names[-3:] == ("y", "raw_s", "actual_s")


def test_the_schema_fingerprint_is_pinned() -> None:
    assert SCHEMA_FINGERPRINTS[(DATASET_VERSION, FEATURE_VERSION)] == (
        schema_fingerprint()
    ), "the feature list or terms changed: bump FEATURE_VERSION / DATASET_VERSION"
    assert ds.COLUMN_FINGERPRINTS[DATASET_VERSION] == ds.column_fingerprint(), (
        "the dataset columns changed: bump DATASET_VERSION"
    )
