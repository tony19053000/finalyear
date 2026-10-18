"""V0.9 G4 lane A item 0 - tester round 1 (independent of the implementer's tests).

Derived from the brief 5e items 0.2/0.3/0.4/0.9/0.10/0.12 and the TELEMETRY.md
contract: schema v2 is v1 plus exactly three nullable columns, hostile
``condition`` / ``condition_measured`` / ``result_schema_version`` input never
reaches PostgreSQL unsanitised, the streaming export is byte-identical to the
buffered one and safe to abandon, the job-side runner agrees with the library
over a whole grid of cnn_train corners.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq

from meshforge.bench.workloads import ParamsError, get_family
from meshforge.bench.workloads.kernels import runner
from meshforge.bench.workloads.kernels._support import KernelParamsError
from meshforge.controller.telemetry import export
from meshforge.controller.telemetry.record import RecordSource, build_record
from meshforge.controller.telemetry.schema import (
    COLUMN_NAMES,
    COLUMNS,
    SCHEMA_FINGERPRINTS,
    SCHEMA_VERSION,
    fingerprint,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)
V2_NEW = ("result_schema_version", "condition", "condition_measured")
ROOT = Path(__file__).resolve().parents[3]


def _src(**kw: Any) -> RecordSource:
    base: dict[str, Any] = {
        "attempt_id": "att_1",
        "job_id": "job_1",
        "attempt_no": 1,
        "node_id": "node_a",
        "state": "done",
        "created_at": T0,
        "ended_at": T0 + dt.timedelta(seconds=9),
        "exit_code": 0,
    }
    base.update(kw)
    return RecordSource(**base)


def _build(source: RecordSource) -> dict[str, Any]:
    rec = build_record(source)
    rec["recorded_at"] = T0 + dt.timedelta(minutes=1)  # the recorder stamps it
    return rec


def _wl(**kw: Any) -> dict[str, Any]:
    return {"family": "cpu.matmul", "status": "ok", "params": {"n": 4}, **kw}


# ------------------------------------------------------------ schema v1 -> v2


def test_v2_is_exactly_v1_plus_the_three_columns() -> None:
    """Removing the three new columns gives back the published v1 fingerprint
    (so every v1 consumer's column set + order is untouched)."""
    v1 = tuple(c for c in COLUMNS if c.name not in V2_NEW)
    assert len(v1) == len(COLUMNS) - 3
    assert fingerprint(v1) == SCHEMA_FINGERPRINTS[1]
    assert (
        fingerprint(COLUMNS)
        == SCHEMA_FINGERPRINTS[SCHEMA_VERSION]
        == SCHEMA_FINGERPRINTS[2]
    )
    assert set(SCHEMA_FINGERPRINTS) == {1, 2}


def test_new_columns_are_nullable_and_unique() -> None:
    by_name = {c.name: c for c in COLUMNS}
    assert len(COLUMN_NAMES) == len(set(COLUMN_NAMES))
    for name in V2_NEW:
        assert by_name[name].nullable


def test_every_column_is_documented_in_telemetry_md() -> None:
    doc = (ROOT / "docs" / "TELEMETRY.md").read_text(encoding="utf-8")
    missing = [n for n in COLUMN_NAMES if f"`{n}`" not in doc]
    assert not missing, missing


def test_new_record_is_stamped_with_current_version() -> None:
    assert build_record(_src())["schema_version"] == SCHEMA_VERSION == 2


# --------------------------------------------- hostile v2 field values (record)


@pytest.mark.parametrize(
    "value",
    [None, "", "x" * 129, "bad\x00nul", "lone\ud800surrogate", 5, 1.5, ["a"], {"a": 1}],
)
def test_condition_hostile_values_are_null(value: Any) -> None:
    assert build_record(_src(condition=value))["condition"] is None


@pytest.mark.parametrize(
    "value",
    [
        "none",
        "cpu_load:2",
        "netem:delay=50ms,loss=1%",
        "x" * 128,
        "é" * 128,
        "名" * 128,
    ],
)
def test_condition_good_values_kept_verbatim(value: str) -> None:
    assert build_record(_src(condition=value))["condition"] == value


@pytest.mark.parametrize(
    "version", [True, False, 0, -1, 2**63, 1.0, 2.5, "1", [1], None, float("nan")]
)
def test_result_schema_version_rejects_non_versions(version: Any) -> None:
    rec = build_record(_src(workload=_wl(result_schema_version=version)))
    assert rec["result_schema_version"] is None


@pytest.mark.parametrize("version", [1, 2, 2**63 - 1])
def test_result_schema_version_accepts_positive_int64(version: int) -> None:
    rec = build_record(_src(workload=_wl(result_schema_version=version)))
    assert rec["result_schema_version"] == version


def test_result_schema_version_absent_is_null_not_error() -> None:
    assert build_record(_src(workload=_wl()))["result_schema_version"] is None
    assert build_record(_src())["result_schema_version"] is None


def test_condition_measured_nonfinite_nested_become_null_and_json_roundtrips() -> None:
    measured = {
        "a": float("nan"),
        "b": [1.0, math.inf, {"c": -math.inf}],
        "d": {"e": 1.5, "f": True, "g": None},
    }
    out = build_record(_src(condition="cpu_load:2", condition_measured=measured))
    got = out["condition_measured"]
    assert got == {
        "a": None,
        "b": [1.0, None, {"c": None}],
        "d": {"e": 1.5, "f": True, "g": None},
    }
    json.dumps(got, allow_nan=False)  # what JSONB / the export needs
    assert measured["a"] != measured["a"]  # input not mutated


@pytest.mark.parametrize("value", ["text", 5, [1, 2], None, True])
def test_condition_measured_non_object_is_null(value: Any) -> None:
    assert build_record(_src(condition_measured=value))["condition_measured"] is None


def test_condition_measured_hostile_keys_and_depth_are_null_not_raised() -> None:
    deep: Any = {"x": 1}
    for _ in range(64):
        deep = {"k": deep}
    for bad in (
        {"nul\x00key": 1},
        {"k": "v\x00"},
        {"k": "\ud800"},
        {1: "int key"},
        deep,
    ):
        assert build_record(_src(condition_measured=bad))["condition_measured"] is None


def test_condition_measured_with_huge_flat_object_is_kept_or_null_never_raises() -> (
    None
):
    big = {f"k{i}": i for i in range(5000)}
    got = build_record(_src(condition_measured=big))["condition_measured"]
    assert got in (None, big)


# -------------------------------------------------------------- params (0.9)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (4, 4),
        (
            2**70,
            None,
        ),  # outside int64 in the flat p_ column; params keep or null, never raise
    ],
)
def test_flat_param_column_for_huge_int_never_raises(value: int, expected: Any) -> None:
    rec = build_record(_src(workload=_wl(params={"n": value})))
    assert rec["p_n"] in (expected, None)


def test_params_nan_inf_list_dict_bool_none() -> None:
    p = {
        "n": float("nan"),
        "repeats": [1],
        "size_mb": {"a": 1},
        "level": True,
        "limit": None,
    }
    rec = build_record(_src(workload=_wl(family="cpu.matmul", params=p)))
    wp = rec["workload_params"]
    json.dumps(wp, allow_nan=False)
    assert "repeats" not in wp and "size_mb" not in wp
    assert wp.get("n", "absent") is None


def test_params_unknown_family_keeps_nothing_and_does_not_raise() -> None:
    rec = build_record(_src(workload=_wl(family="no.such", params={"n": 1})))
    assert rec["workload_params"] is None


# -------------------------------------------------------------- export stream


def _records(count: int, *, v1_every: int = 0) -> list[dict[str, Any]]:
    out = []
    for i in range(count):
        rec = _build(
            _src(
                attempt_id=f"att_{i:06d}",
                job_id=f"job_{i}",
                condition=None
                if (v1_every and i % v1_every == 0)
                else f"queue:{i % 5}",
                condition_measured=None
                if (v1_every and i % v1_every == 0)
                else {"depth": float(i)},
                workload=_wl(result_schema_version=1),
            )
        )
        if v1_every and i % v1_every == 0:  # what a pre-0023 row looks like
            rec["schema_version"] = 1
            for name in V2_NEW:
                rec[name] = None
        out.append(rec)
    return out


@pytest.mark.parametrize("count", [0, 1, 2, 5])
def test_stream_equals_buffered_file_with_default_row_groups(count: int) -> None:
    recs = _records(count, v1_every=2)
    streamed = b"".join(export.iter_parquet(recs))
    buffered = export.write_parquet  # the D222 reference path
    import io

    sink = io.BytesIO()
    buffered(export.records_table(recs), sink)
    assert streamed == sink.getvalue()
    assert (
        hashlib.sha256(streamed).digest()
        == hashlib.sha256(export.parquet_bytes(recs)).digest()
    )


def test_stream_one_row_over_a_row_group_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for size in (1, 2, 3):
        monkeypatch.setattr(export, "ROW_GROUP_SIZE", size)
        for count in (size - 1, size, size + 1, 2 * size + 1):
            recs = _records(count)
            pieces = list(export.iter_parquet(recs))
            import io

            sink = io.BytesIO()
            export.write_parquet(export.records_table(recs), sink)
            assert b"".join(pieces) == sink.getvalue(), (size, count)


def test_stream_is_input_order_independent_and_does_not_mutate_by_default() -> None:
    recs = _records(7)
    shuffled = list(reversed(recs))
    before = [r["attempt_id"] for r in shuffled]
    a = b"".join(export.iter_parquet(shuffled))
    assert [r["attempt_id"] for r in shuffled] == before  # copy sorted, not caller's
    assert a == b"".join(export.iter_parquet(recs))


def test_in_place_sort_with_non_list_input_does_not_crash() -> None:
    recs = _records(4)
    from_tuple = b"".join(
        export.iter_parquet(tuple(reversed(recs)), sort_in_place=True)
    )
    from_gen = b"".join(
        export.iter_parquet((r for r in reversed(recs)), sort_in_place=True)
    )
    assert from_tuple == from_gen == b"".join(export.iter_parquet(recs))


def test_abandoning_the_stream_midway_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export, "ROW_GROUP_SIZE", 2)
    gen = export.iter_parquet(_records(9))
    first = next(gen)
    assert first
    gen.close()  # client hung up: must not raise or hang
    with pytest.raises(StopIteration):
        next(gen)


def test_stream_failure_midway_raises_to_the_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export, "ROW_GROUP_SIZE", 2)
    recs = _records(6)
    recs[4] = {
        **recs[4],
        "condition_measured": object(),
    }  # not JSON-able: fails in group 3
    gen = export.iter_parquet(recs)
    got = [next(gen), next(gen)]
    assert all(got)
    with pytest.raises(Exception):  # noqa: B017 - any error, never silent truncation
        list(gen)


def test_mixed_version_export_is_readable_and_typed(tmp_path: Path) -> None:
    recs = _records(10, v1_every=3)
    path = tmp_path / "x.parquet"
    path.write_bytes(b"".join(export.iter_parquet(recs)))
    table = pq.read_table(path)
    assert table.column_names == list(COLUMN_NAMES)
    versions = table.column("schema_version").to_pylist()
    assert set(versions) == {1, 2}
    for version, cond, meas in zip(
        versions,
        table.column("condition").to_pylist(),
        table.column("condition_measured").to_pylist(),
        strict=True,
    ):
        if version == 1:
            assert cond is None and meas is None
        else:
            assert cond is not None and json.loads(meas)["depth"] >= 0.0


def test_exported_condition_measured_is_canonical_json() -> None:
    rec = _build(_src(condition="x", condition_measured={"b": 1, "a": float("nan")}))
    table = export.records_table([rec])
    cell = table.column("condition_measured").to_pylist()[0]
    assert cell == '{"a":null,"b":1}'


# ------------------------------------------- job side cnn_train cap (0.2) grid


def test_runner_agrees_with_library_over_the_whole_grid() -> None:
    family = get_family("ml.cnn_train")
    for batch, width, image in itertools.product(
        (1, 2, 3, 16, 17, 64, 65, 66, 256, 512),
        (1, 8, 32, 33, 128, 254, 255, 256),
        (32, 64),
    ):
        params = {
            "batch": batch,
            "width": width,
            "depth": 2,
            "image": image,
            "steps": 20,
        }
        try:
            family.parse_params(params)
            lib_cap = False
            lib_ok = True
        except ParamsError as exc:
            lib_cap = "first-loss" in str(exc)
            lib_ok = False
        try:
            runner.check_params("ml.cnn_train", params)
            job_ok = True
            job_cap = False
        except KernelParamsError as exc:
            job_ok = False
            job_cap = "first-loss" in str(exc)
        over = batch * width * image * image > 2**24
        assert job_cap == over, (batch, width, image)
        if lib_cap:
            assert not job_ok, (batch, width, image)
        if lib_ok:
            assert job_ok, (batch, width, image)


@pytest.mark.parametrize(
    "bad",
    [
        {"batch": True, "width": 8, "image": 32},
        {"batch": 2**200, "width": 8, "image": 32},
        {"batch": 8.0, "width": 8, "image": 32},
        {"batch": "8", "width": 8, "image": 32},
        {"batch": -1, "width": 8, "image": 32},
        {"batch": 0, "width": 8, "image": 32},
    ],
)
def test_runner_refuses_malformed_cnn_train_params_with_kernel_error_only(
    bad: dict[str, Any],
) -> None:
    with pytest.raises(KernelParamsError):
        runner.check_params("ml.cnn_train", {**bad, "depth": 2, "steps": 20})


def test_runner_cap_does_not_touch_other_families_or_missing_keys() -> None:
    runner.check_params("ml.cnn_train", {"batch": 512})  # others absent: kernel reports
    runner.check_params(
        "ml.cnn_infer",
        {"batch": 512, "width": 256, "image": 64, "depth": 2, "batches": 5},
    )
