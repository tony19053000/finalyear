"""V0.9 G1 fix round 1: ``compute_iterations`` (D228 merge requirement) and the
storability of untrusted workload values (T-2), end to end in pure code.

The merge test runs a **real** G2 benchmark kernel (``meshforge.bench``), lets
the bench runner write ``results/workload.json`` exactly as a job does, and
pushes it through every G1 stage: the agent reader, the proto, the
controller's report parser, the run-record builder and the Parquet export -
nothing is refused or stripped.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution.workload_result import (
    MAX_COMPUTE_ITERATIONS,
    read_workload_result,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.UTC)
DOC: dict[str, Any] = {
    "schema_version": 1,
    "family": "cpu.matmul",
    "params": {"n": 8, "repeats": 22, "dtype": "float64"},
    "seed": 7,
    "status": "ok",
    "device": "cpu",
    "compute_ns": 1_000_000,
    "compute_iterations": 22,
}


def _write(workdir: Path, doc: object) -> None:
    target = workdir / "results" / "workload.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc), encoding="utf-8")


def _status(report: agent_pb2.WorkloadReport, compute_ns: int | None) -> Any:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE,
    )
    status.phases.run_s = 1.0
    if compute_ns is not None:
        status.phases.compute_ns = compute_ns
    status.workload.CopyFrom(report)
    return status


# ------------------------------------------------------------- agent reader


def test_the_bound_is_the_largest_loop_parameter_of_any_family() -> None:
    from meshforge.bench.workloads.kernels.runner import ITERATION_PARAM, PARAM_LIMITS

    loops = [
        PARAM_LIMITS[family][name]
        for family, name in ITERATION_PARAM.items()
        if name is not None
    ]
    assert max(loops) == MAX_COMPUTE_ITERATIONS
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.reports import MAX_WORKLOAD_ITERATIONS

    assert MAX_WORKLOAD_ITERATIONS == MAX_COMPUTE_ITERATIONS


def test_compute_iterations_is_read_and_sent(tmp_path: Path) -> None:
    _write(tmp_path, DOC)
    report = read_workload_result(tmp_path)
    assert report is not None and report.compute_iterations == 22
    assert report.to_proto().compute_iterations == 22


@pytest.mark.parametrize(
    "value",
    [0, -1, MAX_COMPUTE_ITERATIONS + 1, 22.0, "22", True, [22], {"n": 1}, 2**70],
    ids=["zero", "neg", "over", "float", "str", "bool", "list", "dict", "huge"],
)
def test_a_bad_compute_iterations_is_null_never_an_error(
    tmp_path: Path, value: object
) -> None:
    _write(tmp_path, {**DOC, "compute_iterations": value})
    report = read_workload_result(tmp_path)
    assert report is not None and report.compute_iterations is None
    assert not report.to_proto().HasField("compute_iterations")
    assert report.compute_ns == 1_000_000  # the rest of the report survives


def test_the_maximum_is_accepted(tmp_path: Path) -> None:
    _write(tmp_path, {**DOC, "compute_iterations": MAX_COMPUTE_ITERATIONS})
    report = read_workload_result(tmp_path)
    assert report is not None and report.compute_iterations == MAX_COMPUTE_ITERATIONS


def test_iterations_without_compute_ns_are_null(tmp_path: Path) -> None:
    _write(tmp_path, {**DOC, "compute_ns": None, "status": "unavailable"})
    report = read_workload_result(tmp_path)
    assert report is not None and report.compute_iterations is None


@pytest.mark.parametrize(
    "params",
    [{"note": "a\u0000b"}, {"a\u0000": 1}, {"x": ["\ud800"]}, {"\udfff": 1}],
    ids=["nul-value", "nul-key", "surrogate-nested", "surrogate-key"],
)
def test_params_that_postgres_cannot_store_are_dropped(
    tmp_path: Path, params: dict[str, Any]
) -> None:
    target = tmp_path / "results" / "workload.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({**DOC, "params": params}), encoding="ascii")
    report = read_workload_result(tmp_path)
    assert report is not None and report.params_json == ""
    assert report.status == "ok" and report.compute_iterations == 22


# ----------------------------------------------------- controller + record


def test_the_controller_refuses_out_of_range_or_orphan_iterations() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.reports import parse_status

    ok = agent_pb2.WorkloadReport(family="cpu.matmul", status="ok", device="cpu")
    ok.compute_iterations = 22
    parsed = parse_status(_status(ok, 10))
    assert not isinstance(parsed, str), parsed
    assert parsed.workload is not None and parsed.workload["compute_iterations"] == 22
    for bad, ns in ((0, 10), (MAX_COMPUTE_ITERATIONS + 1, 10), (5, None)):
        report = agent_pb2.WorkloadReport(family="cpu.x", status="ok")
        report.compute_iterations = bad
        assert isinstance(parse_status(_status(report, ns)), str), (bad, ns)


@pytest.mark.parametrize(
    "params_json",
    ['{"a":"x\\u0000y"}', '{"\\u0000":1}', '{"a":["\\ud800"]}'],
    ids=["nul", "nul-key", "surrogate"],
)
def test_the_controller_refuses_params_postgres_cannot_store(params_json: str) -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.reports import parse_status

    report = agent_pb2.WorkloadReport(
        family="cpu.x", status="ok", params_json=params_json
    )
    assert isinstance(parse_status(_status(report, 10)), str)


def _source(workload: dict[str, Any]) -> Any:
    from meshforge.controller.telemetry.record import RecordSource

    return RecordSource(
        attempt_id="att_1",
        job_id="job_1",
        attempt_no=1,
        node_id="node_b",
        state="done",
        created_at=T0,
        ended_at=T0 + dt.timedelta(seconds=3),
        exit_code=0,
        workload=workload,
    )


def test_the_record_carries_iterations_only_with_compute_ns() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.telemetry.record import build_record

    base = {"family": "cpu.matmul", "status": "ok", "compute_ns": 10}
    assert (
        build_record(_source({**base, "compute_iterations": 4}))["compute_iterations"]
        == 4
    )
    assert (
        build_record(_source({**base, "compute_ns": None, "compute_iterations": 4}))[
            "compute_iterations"
        ]
        is None
    )
    for bad in (0, -3, True, "4", 4.0):
        record = build_record(_source({**base, "compute_iterations": bad}))
        assert record["compute_iterations"] is None, bad


def test_the_record_nulls_values_postgres_would_refuse_instead_of_cutting() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.telemetry.record import SHORT_TEXT_CHARS, build_record

    record = build_record(
        _source(
            {
                "family": "f" * (SHORT_TEXT_CHARS + 1),
                "status": "ok",
                "device": "cpu",
                "params": {
                    "dtype": "d" * 65,
                    "n": 4,
                    "deep": json.loads("[" * 40 + "]" * 40),
                },
            }
        )
    )
    assert record["workload_family"] is None and record["p_dtype"] is None
    assert record["workload_device"] == "cpu"
    assert record["workload_params"] is None  # deeper than MAX_JSON_DEPTH
    record = build_record(
        _source({"status": "ok", "params": {"dtype": "d" * 64, "n": 4}})
    )
    assert record["p_dtype"] == "d" * 64 and record["p_n"] == 4
    assert record["workload_params"] == {"dtype": "d" * 64, "n": 4}


# ------------------------------------------------- the D228 merge test


@pytest.mark.parametrize(
    ("family", "params"),
    [
        ("cpu.matmul", {"n": 8, "repeats": 3, "dtype": "float64"}),
        ("cpu.sha256", {"size_mb": 1, "chunk_kb": 64}),
    ],
)
def test_a_real_g2_bench_result_is_not_refused_or_stripped(
    tmp_path: Path, family: str, params: dict[str, Any]
) -> None:
    pytest.importorskip("numpy", reason="the cpu kernels need numpy")
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    import pyarrow.parquet as pq

    from meshforge.bench.workloads.kernels import runner
    from meshforge.controller.jobs.reports import parse_status
    from meshforge.controller.telemetry.export import parquet_bytes
    from meshforge.controller.telemetry.record import build_record

    (tmp_path / runner.PARAMS_FILE).write_text(
        json.dumps({"family": family, "params": params, "seed": 5}), encoding="utf-8"
    )
    assert runner.main(tmp_path) == 0  # the job entrypoint, as in a real job
    written = json.loads((tmp_path / runner.RESULT_FILE).read_text("utf-8"))
    expected_iterations = runner.compute_iterations(family, params)
    assert written["compute_iterations"] == expected_iterations is not None

    report = read_workload_result(tmp_path)
    assert report is not None
    assert (report.family, report.status, report.device, report.seed) == (
        family,
        "ok",
        "cpu",
        5,
    )
    assert json.loads(report.params_json) == params
    assert report.compute_ns == written["compute_ns"]
    assert report.compute_iterations == expected_iterations
    assert report.result_schema_version == runner.RESULT_SCHEMA_VERSION

    parsed = parse_status(_status(report.to_proto(), report.compute_ns))
    assert not isinstance(parsed, str), parsed
    assert parsed.workload is not None
    assert parsed.workload["params"] == params
    assert parsed.workload["compute_iterations"] == expected_iterations

    record = build_record(_source(parsed.workload))
    assert record["workload_family"] == family
    assert record["workload_params"] == params
    assert record["compute_ns"] == written["compute_ns"]
    assert record["compute_iterations"] == expected_iterations
    assert record["failure_class"] == "ok"

    table = pq.read_table(io.BytesIO(parquet_bytes([{**record, "recorded_at": T0}])))
    row = table.to_pylist()[0]
    assert row["compute_iterations"] == expected_iterations
    assert row["compute_ns"] == written["compute_ns"]
    assert json.loads(row["workload_params"]) == params
