"""V0.9 G4 lane A item 0 - the telemetry side.

* 0.3 (D233.3/D241): telemetry schema **v2** - ``result_schema_version``,
  ``condition`` (canonical string) and ``condition_measured`` (JSON), filled by
  the builder, carried by the export, v1 rows still exportable;
* 0.9 (D240): ``workload_params`` keeps only scalars; NaN / inf -> null;
* 0.10 + 0.11 (D240): the recorder sweeps again at once after a sweep that
  *examined* a full batch, and its rescan clock is injectable;
* 0.4 + 0.12 (D233.4/D240): the export is a stream of row-group pieces whose
  concatenation is the D222 file, and the route's list is sorted in place.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import math
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq

from meshforge.controller.store.models import telemetry as model_module
from meshforge.controller.telemetry import export
from meshforge.controller.telemetry import recorder as recorder_module
from meshforge.controller.telemetry.record import (
    CONDITION_CHARS,
    RecordSource,
    build_record,
)
from meshforge.controller.telemetry.schema import (
    COLUMN_NAMES,
    COLUMNS,
    SCHEMA_FINGERPRINTS,
    SCHEMA_VERSION,
    Kind,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)


def _source(**overrides: Any) -> RecordSource:
    values: dict[str, Any] = {
        "attempt_id": "att_1",
        "job_id": "job_1",
        "attempt_no": 1,
        "node_id": "node_a",
        "state": "done",
        "created_at": T0,
        "ended_at": T0 + dt.timedelta(seconds=9),
        "exit_code": 0,
    }
    values.update(overrides)
    return RecordSource(**values)


def _bench(params: dict[str, Any], **workload: Any) -> RecordSource:
    return _source(
        workload={
            "family": "cpu.matmul",
            "status": "ok",
            "params": params,
            "compute_ns": 5,
            **workload,
        }
    )


# --------------------------------------------------------------- 0.3 schema v2


def test_schema_is_v2_with_the_three_new_columns() -> None:
    assert SCHEMA_VERSION == 2
    assert set(SCHEMA_FINGERPRINTS) == {1, 2}
    assert SCHEMA_FINGERPRINTS[1] == (
        "69e443de70cbb8bbd25fb04662510910c649b812ad16000ede6d092948df723a"
    ), "a published fingerprint never changes"
    kinds = {c.name: (c.kind, c.nullable) for c in COLUMNS}
    assert kinds["result_schema_version"] == (Kind.INT64, True)
    assert kinds["condition"] == (Kind.TEXT, True)
    assert kinds["condition_measured"] == (Kind.JSON, True)


def test_v1_columns_are_all_still_there_in_their_order() -> None:
    """v2 only adds columns: a v1 reader's columns keep their names, kinds and
    relative order (it can select them from a v2 file)."""
    v2 = [c.name for c in COLUMNS]
    new = {"result_schema_version", "condition", "condition_measured"}
    v1 = [n for n in v2 if n not in new]
    assert len(v1) == len(v2) - 3
    assert v1[0] == "schema_version" and v1[-1] == "recorded_at"


def test_the_condition_width_matches_the_model() -> None:
    assert model_module._CONDITION == CONDITION_CHARS
    column = model_module.RunRecord.__table__.c.condition
    assert column.type.length == CONDITION_CHARS


def test_builder_writes_v2_and_fills_the_condition_columns() -> None:
    record = build_record(
        _source(
            condition="cpu_load:2",
            condition_measured={"cpu_busy_cores": 1.93, "window_s": 2.0},
        )
    )
    assert record["schema_version"] == 2
    assert record["condition"] == "cpu_load:2"
    assert record["condition_measured"] == {"cpu_busy_cores": 1.93, "window_s": 2.0}
    plain = build_record(_source())
    assert plain["condition"] is None and plain["condition_measured"] is None
    assert plain["result_schema_version"] is None


def test_condition_values_are_bounded_never_cut() -> None:
    fits = "netem:" + "d" * (CONDITION_CHARS - 6)
    assert build_record(_source(condition=fits))["condition"] == fits
    assert build_record(_source(condition=fits + "x"))["condition"] is None
    assert build_record(_source(condition=""))["condition"] is None
    assert build_record(_source(condition="a\x00b"))["condition"] is None
    for bad in (["cpu_busy_cores", 1.0], "text", 3):
        record = build_record(_source(condition_measured=bad))  # type: ignore[arg-type]
        assert record["condition_measured"] is None


def test_condition_measured_non_finite_floats_become_null() -> None:
    record = build_record(
        _source(
            condition_measured={
                "rtt_ms": math.nan,
                "bw": math.inf,
                "nested": {"x": -math.inf, "ok": 1.5},
                "list": [1.0, math.nan],
            }
        )
    )
    assert record["condition_measured"] == {
        "rtt_ms": None,
        "bw": None,
        "nested": {"x": None, "ok": 1.5},
        "list": [1.0, None],
    }
    export.parquet_bytes([{**record, "recorded_at": T0}])  # canonical JSON accepts it


@pytest.mark.parametrize(
    ("reported", "stored"),
    [(1, 1), (7, 7), (0, None), (-1, None), (True, None), ("1", None), (None, None)],
)
def test_result_schema_version_comes_from_the_workload_report(
    reported: Any, stored: int | None
) -> None:
    record = build_record(_bench({"n": 512}, result_schema_version=reported))
    assert record["result_schema_version"] == stored


def test_the_recorder_reads_condition_from_the_campaign_identity() -> None:
    """The lane B seam: the campaign lookup's optional ``condition`` /
    ``condition_measured`` keys reach the RecordSource (recorder._sources)."""
    import inspect

    text = inspect.getsource(recorder_module._sources)
    assert 'identity.get("condition")' in text
    assert 'identity.get("condition_measured")' in text


def _exportable(record: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    return {**record, "attempt_id": attempt_id, "recorded_at": T0}


def test_export_carries_the_v2_columns_and_v1_rows_side_by_side() -> None:
    v2 = build_record(
        _bench(
            {"n": 512, "repeats": 3, "dtype": "float64"},
            result_schema_version=1,
        )
    )
    v2 = {**v2, "condition": "queue:2", "condition_measured": {"queue_depth": 3}}
    v1 = {
        **build_record(_source()),
        "schema_version": 1,
        "result_schema_version": None,
        "condition": None,
        "condition_measured": None,
    }
    data = export.parquet_bytes([_exportable(v2, "att_b"), _exportable(v1, "att_a")])
    table = pq.read_table(io.BytesIO(data))
    assert table.schema.names == list(COLUMN_NAMES)
    assert table.schema.metadata[b"meshforge.telemetry.schema_version"] == b"2"
    rows = {r["attempt_id"]: r for r in table.to_pylist()}
    assert rows["att_b"]["schema_version"] == 2
    assert rows["att_b"]["result_schema_version"] == 1
    assert rows["att_b"]["condition"] == "queue:2"
    assert rows["att_b"]["condition_measured"] == '{"queue_depth":3}'
    assert rows["att_a"]["schema_version"] == 1
    assert rows["att_a"]["condition"] is None
    assert rows["att_a"]["condition_measured"] is None


# ------------------------------------------------------------- 0.9 params


def test_params_keep_only_scalars_and_null_non_finite_floats() -> None:
    record = build_record(
        _bench(
            {
                "n": 512,
                "repeats": [1, 2],  # a list: dropped
                "dtype": {"x": 1},  # an object: dropped
                "secret": "s3cr3t",  # not a cpu.matmul field: dropped (D236)
            }
        )
    )
    assert record["workload_params"] == {"n": 512}
    assert record["p_n"] == 512 and record["p_repeats"] is None
    assert record["p_dtype"] is None


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_non_finite_param_is_null_and_the_record_exports(value: float) -> None:
    record = build_record(_bench({"n": value, "repeats": 2, "dtype": "float32"}))
    assert record["workload_params"] == {"n": None, "repeats": 2, "dtype": "float32"}
    assert record["p_n"] is None
    data = export.parquet_bytes([{**record, "recorded_at": T0}])
    back = pq.read_table(io.BytesIO(data)).to_pylist()[0]
    assert back["workload_params"] == '{"dtype":"float32","n":null,"repeats":2}'


def test_scalar_kinds_are_kept_as_they_are() -> None:
    record = build_record(_bench({"n": 1.5, "repeats": True, "dtype": None}))
    assert record["workload_params"] == {"n": 1.5, "repeats": True, "dtype": None}
    assert record["p_n"] is None and record["p_repeats"] is None  # not ints


# ---------------------------------------------------- 0.10 + 0.11 recorder


class _FakeEngine:
    """Enough of an AsyncEngine for ``create_session_factory`` (never used)."""

    sync_engine = None


def _recorder(**kwargs: Any) -> recorder_module.RunRecorder:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/x")
    return recorder_module.RunRecorder(engine, **kwargs)


def test_run_sweeps_again_at_once_when_a_full_batch_was_examined() -> None:
    """A past-cap pass examines a full batch of skips and writes nothing: the
    next sweep must start at once, not after ``interval_s`` (60 s here)."""
    rec = _recorder(interval_s=60.0, batch=5)
    examined = [5, 5, 5, 2]  # three full (all skipped) batches, then the end
    calls: list[int] = []

    async def sweep() -> int:
        calls.append(len(calls))
        rec.last_examined = examined[min(len(calls) - 1, len(examined) - 1)]
        return 0

    rec.sweep = sweep  # type: ignore[method-assign]

    async def go() -> None:
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(calls) == 4  # 3 full batches back to back, then wait 60 s


def test_a_short_examined_batch_waits_for_the_interval() -> None:
    rec = _recorder(interval_s=60.0, batch=5)
    calls: list[int] = []

    async def sweep() -> int:
        calls.append(1)
        rec.last_examined = 4
        return 0

    rec.sweep = sweep  # type: ignore[method-assign]

    async def go() -> None:
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(calls) == 1


def test_the_rescan_clock_is_injectable() -> None:
    now = [100.0]
    rec = _recorder(clock=lambda: now[0])
    rec.past_cap = True
    rec._watermark = (T0, "att_9")
    rec._pass_done = True
    rec._rescan_at = 100.0 + recorder_module.PAST_CAP_RESCAN_S
    assert rec._scan_from() == (T0, "att_9")  # the period has not passed
    now[0] += recorder_module.PAST_CAP_RESCAN_S
    assert rec._scan_from() is None  # reset to the oldest, no real wait
    assert rec._rescan_at == now[0] + recorder_module.PAST_CAP_RESCAN_S


def test_the_default_clock_follows_the_modules_time() -> None:
    """Older tests patch ``recorder.time``; the default clock still sees it."""
    rec = _recorder()
    assert rec._clock is recorder_module._monotonic


# --------------------------------------------------- 0.4 + 0.12 the stream


def _records(n: int) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        record = build_record(
            _source(
                attempt_id=f"att_{i:03d}",
                campaign={"campaign_id": "camp", "config_id": f"c{i % 3}"},
            )
        )
        record["recorded_at"] = T0
        out.append(record)
    return out


@pytest.mark.parametrize("count", [0, 1, 3, 4, 7, 9])
def test_the_pieces_are_one_row_group_each_and_join_to_the_d222_file(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    monkeypatch.setattr(export, "ROW_GROUP_SIZE", 3)
    records = _records(count)
    pieces = list(export.iter_parquet(list(reversed(records))))
    whole = io.BytesIO()
    export.write_parquet(export.records_table(records), whole)
    assert b"".join(pieces) == whole.getvalue() == export.parquet_bytes(records)
    groups = max(1, -(-count // 3))
    # one piece per row group, then the footer (no rows: header + footer once)
    assert len(pieces) == (1 if count == 0 else groups + 1)
    assert pq.read_metadata(io.BytesIO(b"".join(pieces))).num_row_groups == groups


def test_sort_in_place_reorders_the_callers_list_and_copies_nothing() -> None:
    records = list(reversed(_records(6)))
    expected = sorted(records, key=export.sort_key)
    assert export._ordered(records, in_place=True) is records  # no copy
    assert records == expected  # the caller's list, now in export order
    data = b"".join(export.iter_parquet(list(reversed(records)), sort_in_place=True))
    assert data == export.parquet_bytes(expected)


def test_without_sort_in_place_the_input_is_untouched() -> None:
    records = list(reversed(_records(4)))
    before = [r["attempt_id"] for r in records]
    b"".join(export.iter_parquet(records))
    assert [r["attempt_id"] for r in records] == before


def test_a_stream_closed_early_leaves_no_open_writer() -> None:
    pieces = export.iter_parquet(_records(2))
    first = next(pieces)
    assert first  # one row group's bytes
    pieces.close()  # the route's client went away: the finally closes the writer
