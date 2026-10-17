"""V0.9 G1 items 3 + 5 (D212, D222): the run-record builder and the export."""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.telemetry.record import (
    PhaseValue,
    RecordSource,
    TransferRow,
    build_record,
    failure_class,
)
from meshforge.controller.telemetry.schema import COLUMNS, FailureClass

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.UTC)

SNAPSHOT: dict[str, Any] = {
    "version": 2,
    "node": {"state": "online", "rtt_ms": 1.5, "capabilities": ["exec"]},
    "resources": {
        "cpu_percent": 12.5,
        "ram_used_bytes": 3 * 2**30,
        "ram_total_bytes": 16 * 2**30,
        "gpus": [
            {
                "index": 0,
                "util_percent": 5.0,
                "vram_used_bytes": 1,
                "vram_total_bytes": 9,
            }
        ],
        "age_s": 0.4,
    },
    "scores": {
        "metrics": [
            {"name": "cpu_fp32_gflops_all", "status": "ok", "mean": 120.0},
            {"name": "gpu_fp32_tflops", "status": "ok", "mean": 9.5},
        ]
    },
    "gpu_indices": [0],
    "telemetry": {
        "node_live_attempts": 2,
        "cache": [
            {"tree_id": "sha256:" + "a" * 64, "bytes": 100, "warm": True},
            {"tree_id": "sha256:" + "b" * 64, "bytes": 50, "warm": False},
        ],
        "network": {"source": "peer", "rtt_ms": 3.0, "bw_bytes_s": 1e7, "samples": 4},
        "clock": {
            "clock_offset_ms": 5000.25,
            "clock_offset_bound_ms": 1.5,
            "consistent": True,
        },
    },
}


def _source(**overrides: Any) -> RecordSource:
    values: dict[str, Any] = {
        "attempt_id": "att_1",
        "job_id": "job_1",
        "attempt_no": 1,
        "node_id": "node_b",
        "state": "done",
        "created_at": T0,
        "ended_at": T0 + dt.timedelta(seconds=9),
        "exit_code": 0,
        "runtime": "venv",
        "output_bytes": 1234,
        "start_snapshot": SNAPSHOT,
        "workload": {
            "family": "cpu.matmul",
            "params": {"n": 512, "repeats": 3, "dtype": "float64"},
            "seed": 7,
            "status": "ok",
            "device": "cpu",
            "compute_ns": 123_456_789,
        },
        "manifest": {"env": {"base": "python-cpu"}, "resources": {"gpu": "none"}},
        "placement": {"mode": "offload", "node_id": "node_b"},
        "submitter_node_id": "node_a",
        "fingerprint": "f" * 64,
        "phases": {
            "queue": PhaseValue(40, False),
            "stage": PhaseValue(300, False),
            "env": PhaseValue(0, True),
            "run": PhaseValue(2000, False),
            "collect": PhaseValue(60, False),
            "compute": PhaseValue(123, False),
        },
        "decision_policy": "fastest",
        "decision_chosen": "node_b",
        "decision_candidates": [
            {"node_id": "node_b", "estimate": {"total_s": 2.5}},
            {"node_id": "node_a", "estimate": {"total_s": 3.0}},
        ],
        "transfers": [TransferRow("controller", 70), TransferRow("cache", 30)],
    }
    values.update(overrides)
    return RecordSource(**values)


def test_a_bench_record_carries_every_d212_field() -> None:
    r = build_record(_source())
    assert r["schema_version"] == 1 and r["failure_class"] == "ok"
    assert r["strategy"] == "mode1" and r["placement_mode"] == "pinned"
    assert r["planner_node_id"] is None  # pinned: the planner did not choose
    assert r["planner_estimate_s"] == 2.5
    assert r["workload_family"] == "cpu.matmul" and r["workload_seed"] == 7
    assert (r["p_n"], r["p_repeats"], r["p_dtype"]) == (512, 3, "float64")
    assert r["p_batch"] is None
    assert r["compute_ns"] == 123_456_789
    assert (r["queue_ms"], r["stage_ms"], r["env_ms"], r["run_ms"]) == (
        40,
        300,
        None,  # skipped, never 0
        2000,
    )
    assert r["total_ms"] == 40 + 300 + 2000 + 60
    assert r["node_ram_free_bytes"] == 13 * 2**30
    assert r["node_gpu_vram_free_bytes"] == 8
    assert (r["node_cpu_score"], r["node_gpu_score"]) == (120.0, 9.5)
    assert r["node_live_attempts"] == 2
    assert (r["cache_inputs"], r["cache_warm_inputs"], r["cache_state"]) == (
        2,
        1,
        "partial",
    )
    assert (r["net_source"], r["net_rtt_ms"], r["net_samples"]) == ("peer", 3.0, 4)
    assert (r["bytes_in"], r["bytes_in_cached"], r["bytes_out"]) == (70, 30, 1234)
    assert (r["clock_offset_ms"], r["clock_offset_bound_ms"]) == (5000.25, 1.5)
    assert r["clock_consistent"] is True
    assert (r["env_base"], r["gpu_request"], r["runtime"]) == (
        "python-cpu",
        "none",
        "venv",
    )


def test_a_non_bench_auto_local_job_gets_a_record_too() -> None:
    r = build_record(
        _source(
            node_id="node_a",
            workload=None,
            placement={"mode": "local"},
            start_snapshot=None,
            transfers=(),
            decision_chosen="node_a",
        )
    )
    assert r["strategy"] == "local" and r["placement_mode"] == "auto"
    assert r["planner_node_id"] == "node_a"
    assert r["workload_family"] is None and r["compute_ns"] is None
    assert r["node_cpu_percent"] is None and r["cache_state"] is None
    assert r["bytes_in"] is None and r["clock_offset_ms"] is None
    assert r["failure_class"] == "ok"


@pytest.mark.parametrize(
    ("state", "failure", "exit_code", "workload", "expected"),
    [
        ("done", None, 0, None, FailureClass.OK),
        (
            "failed",
            "user_error",
            3,
            {"status": "unavailable"},
            FailureClass.UNAVAILABLE,
        ),
        ("failed", "user_error", 3, {"status": "garbled"}, FailureClass.UNAVAILABLE),
        ("failed", "user_error", 4, {"status": "check_failed"}, FailureClass.WORKLOAD),
        ("failed", "user_error", 2, {"status": "bad_params"}, FailureClass.WORKLOAD),
        ("failed", "user_error", 1, None, FailureClass.WORKLOAD),
        ("failed", "user_error", 3, None, FailureClass.WORKLOAD),  # not a bench
        ("failed", "oom", None, None, FailureClass.WORKLOAD),
        ("failed", "deadline_exceeded", None, None, FailureClass.WORKLOAD),
        ("failed", "cancelled", None, None, FailureClass.UNAVAILABLE),
        ("failed", "env_error", None, None, FailureClass.INFRASTRUCTURE),
        ("failed", "transfer_error", None, None, FailureClass.INFRASTRUCTURE),
        ("lost", "node_lost", None, None, FailureClass.INFRASTRUCTURE),
        ("fenced", None, None, None, FailureClass.INFRASTRUCTURE),
        ("failed", "no_such_type", None, None, FailureClass.INFRASTRUCTURE),
        ("failed", None, None, None, FailureClass.INFRASTRUCTURE),
    ],
)
def test_every_record_gets_a_failure_class(
    state: str,
    failure: str | None,
    exit_code: int | None,
    workload: dict[str, Any] | None,
    expected: FailureClass,
) -> None:
    assert failure_class(state, failure, exit_code, workload) is expected


def test_hostile_snapshot_values_never_reach_a_column_with_the_wrong_type() -> None:
    hostile: dict[str, Any] = {
        "node": {"state": 5, "rtt_ms": "fast"},
        "resources": {
            "cpu_percent": float("nan"),
            "ram_used_bytes": True,
            "ram_total_bytes": 2**80,
            "gpus": "many",
            "age_s": float("inf"),
        },
        "gpu_indices": "0",
        "telemetry": {"cache": "warm", "network": [], "clock": "skewed"},
    }
    r = build_record(
        _source(
            start_snapshot=hostile,
            workload={"params": {"n": "big", "dtype": 3}, "compute_ns": -5},
        )
    )
    for key in (
        "node_state",
        "node_rtt_ms",
        "node_cpu_percent",
        "node_ram_free_bytes",
        "node_ram_total_bytes",
        "node_gpu_util_percent",
        "node_sample_age_s",
        "cache_inputs",
        "net_rtt_ms",
        "clock_offset_ms",
        "p_n",
        "p_dtype",
        "compute_ns",
    ):
        assert r[key] is None, key


# ------------------------------------------------------------------- export


def _records(n: int = 5) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        record = build_record(
            _source(
                attempt_id=f"att_{i:02d}",
                campaign={
                    "campaign_id": None if i == 0 else "camp_1",
                    "config_id": f"cfg_{i % 2}",
                    "repetition": i,
                },
            )
        )
        record["recorded_at"] = T0 + dt.timedelta(minutes=i)
        out.append(record)
    return out


def test_export_twice_gives_identical_bytes_in_any_input_order() -> None:
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    from meshforge.controller.telemetry.export import parquet_bytes

    records = _records()
    first = parquet_bytes(records)
    second = parquet_bytes(list(reversed(records)))
    assert first == second
    assert first[:4] == b"PAR1" and first[-4:] == b"PAR1"


def test_export_round_trip_equals_the_records() -> None:
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    import json

    import pyarrow.parquet as pq

    from meshforge.controller.telemetry.export import parquet_bytes, sort_key

    records = _records()
    table = pq.read_table(io.BytesIO(parquet_bytes(records)))
    assert table.column_names == [c.name for c in COLUMNS]
    back = table.to_pylist()
    expected = sorted(records, key=sort_key)
    json_cols = {c.name for c in COLUMNS if c.kind.value == "json"}
    for got, want in zip(back, expected, strict=True):
        for name in want:
            value = got[name]
            if name in json_cols and value is not None:
                value = json.loads(value)
            assert value == want[name], name
    # stable sort: null campaign first, then (config, repetition)
    assert [r["attempt_id"] for r in back] == [
        "att_00",
        "att_02",
        "att_04",
        "att_01",
        "att_03",
    ]


def test_the_file_holds_no_export_time_value() -> None:
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    import pyarrow.parquet as pq

    from meshforge.controller.telemetry.export import parquet_bytes

    meta = pq.read_metadata(io.BytesIO(parquet_bytes(_records(2))))
    keys = set(meta.metadata or {})
    assert keys <= {b"ARROW:schema", b"meshforge.telemetry.schema_version"}
    assert "parquet-cpp-arrow" in meta.created_by


def test_an_empty_export_is_a_valid_file_with_the_schema() -> None:
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    import pyarrow.parquet as pq

    from meshforge.controller.telemetry.export import parquet_bytes

    data = parquet_bytes([])
    table = pq.read_table(io.BytesIO(data))
    assert table.num_rows == 0 and table.column_names == [c.name for c in COLUMNS]
    assert parquet_bytes([]) == data


def test_a_null_in_a_required_column_is_refused() -> None:
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    from meshforge.controller.telemetry.export import records_table

    record = _records(1)[0]
    record["failure_class"] = None
    with pytest.raises(ValueError, match="failure_class"):
        records_table([record])
