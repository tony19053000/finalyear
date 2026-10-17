"""V0.9 G1 item 2 (D213): the agent reads a benchmark's results/workload.json
(bounded, untrusted, no symlinks) and the controller validates the report."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution.workload_result import (
    MAX_RESULT_BYTES,
    read_workload_result,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2

pytestmark = pytest.mark.unit

DOC: dict[str, Any] = {
    "schema_version": 1,
    "family": "cpu.matmul",
    "params": {"n": 256, "repeats": 2, "dtype": "float64"},
    "seed": 7,
    "status": "ok",
    "device": "cpu",
    "compute_ns": 12_345_678,
    "verified": True,
}


def _write(workdir: Path, doc: object, *, raw: bytes | None = None) -> Path:
    target = workdir / "results" / "workload.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw if raw is not None else json.dumps(doc).encode())
    return target


def test_a_benchmark_result_is_read(tmp_path: Path) -> None:
    _write(tmp_path, DOC)
    report = read_workload_result(tmp_path)
    assert report is not None
    assert report.family == "cpu.matmul" and report.status == "ok"
    assert report.compute_ns == 12_345_678 and report.seed == 7
    assert json.loads(report.params_json) == DOC["params"]
    assert report.params_json == '{"dtype":"float64","n":256,"repeats":2}'
    proto = report.to_proto()
    assert proto.family == "cpu.matmul" and proto.seed == 7
    assert proto.result_schema_version == 1


def test_no_file_is_no_report(tmp_path: Path) -> None:
    assert read_workload_result(tmp_path) is None


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1, 2]", b'{"status": "weird"}', b"\xff\xfe", b"{" * 100_000],
)
def test_garbage_is_no_report_never_an_error(tmp_path: Path, raw: bytes) -> None:
    _write(tmp_path, None, raw=raw)
    assert read_workload_result(tmp_path) is None


def test_a_file_over_the_bound_is_not_read(tmp_path: Path) -> None:
    doc = dict(DOC, padding="x" * MAX_RESULT_BYTES)
    _write(tmp_path, doc)
    assert read_workload_result(tmp_path) is None


@pytest.mark.parametrize(
    ("field", "value", "attr", "expected"),
    [
        ("compute_ns", -1, "compute_ns", None),
        ("compute_ns", True, "compute_ns", None),
        ("compute_ns", 10**30, "compute_ns", None),
        ("compute_ns", 5.0, "compute_ns", 5),
        ("compute_ns", "5", "compute_ns", None),
        ("seed", -3, "seed", None),
        ("family", "Bad Name!", "family", ""),
        ("device", "x" * 100, "device", ""),
        ("params", {"big": "y" * 5000}, "params_json", ""),
        ("params", {"bad": float("nan")}, "params_json", ""),
    ],
)
def test_every_field_is_bounded(
    tmp_path: Path, field: str, value: object, attr: str, expected: object
) -> None:
    raw = json.dumps(dict(DOC, **{field: value})).encode()
    _write(tmp_path, None, raw=raw)
    report = read_workload_result(tmp_path)
    assert report is not None
    assert getattr(report, attr) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_file_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(DOC))
    work = tmp_path / "work"
    (work / "results").mkdir(parents=True)
    os.symlink(outside, work / "results" / "workload.json")
    assert read_workload_result(work) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_results_directory_is_not_followed(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere, DOC)
    work = tmp_path / "work"
    work.mkdir()
    os.symlink(elsewhere / "results", work / "results")
    assert read_workload_result(work) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fifo")
def test_a_fifo_is_not_read_and_never_blocks(tmp_path: Path) -> None:
    (tmp_path / "results").mkdir()
    os.mkfifo(tmp_path / "results" / "workload.json")
    assert read_workload_result(tmp_path) is None


# ------------------------------------------------- controller-side parsing


def _status(**workload: Any) -> agent_pb2.AttemptStatus:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE,
    )
    status.phases.run_s = 1.0
    status.phases.compute_ns = 42
    fields: dict[str, Any] = {
        "family": "cpu.matmul",
        "params_json": '{"n":4}',
        "status": "ok",
        "device": "cpu",
        "result_schema_version": 1,
    }
    fields.update(workload)
    status.workload.CopyFrom(agent_pb2.WorkloadReport(**fields))
    status.workload.seed = 9
    return status


def test_the_controller_accepts_a_valid_report() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.reports import parse_status

    report = parse_status(_status())
    assert not isinstance(report, str), report
    assert report.phases is not None and report.phases.compute_ns == 42
    assert report.workload == {
        "family": "cpu.matmul",
        "params": {"n": 4},
        "seed": 9,
        "status": "ok",
        "device": "cpu",
        "result_schema_version": 1,
        "compute_ns": 42,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"family": "Not A Name"},
        {"status": "fine"},
        {"params_json": "[1]"},
        {"params_json": "NaN"},
        {"params_json": '{"x": Infinity}'},
        {"params_json": "{" + '"a":1,' * 1000 + '"b":2}'},
    ],
)
def test_the_controller_refuses_a_malformed_report(bad: dict[str, Any]) -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.reports import parse_status

    assert isinstance(parse_status(_status(**bad)), str)


def test_a_workload_on_a_non_terminal_report_is_refused() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.reports import parse_status

    status = _status()
    status.state = common_pb2.ATTEMPT_STATE_RUNNING
    assert isinstance(parse_status(status), str)


def test_a_compute_time_over_the_bound_is_refused() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.progress import MAX_PHASE_NS
    from meshforge.controller.jobs.reports import parse_status

    status = _status()
    status.phases.compute_ns = MAX_PHASE_NS + 1
    assert isinstance(parse_status(status), str)


@pytest.mark.security
@pytest.mark.parametrize(
    "leak",
    [
        "s3cr3t-value-123",  # plain
        "czNjcjN0LXZhbHVlLTEyMw==",  # base64, a form the log redaction knows
    ],
)
def test_a_secret_written_into_the_result_never_leaves_the_node(
    tmp_path: Path, leak: str
) -> None:
    """THREAT_MODEL RE-I6: a workload that writes its own secret value into
    results/workload.json (params) cannot smuggle it into the run record and
    the admin's dataset export - the field is dropped, the rest is kept."""
    from meshforge.common.secret_redaction import SecretMatcher

    _write(tmp_path, dict(DOC, params={"n": 4, "note": leak}))
    matcher = SecretMatcher({"API_KEY": b"s3cr3t-value-123"})
    report = read_workload_result(tmp_path, matcher=matcher)
    assert report is not None
    assert report.params_json == "" and report.compute_ns == DOC["compute_ns"]
    assert leak not in repr(report) and leak not in str(report.to_proto())
    clean = read_workload_result(tmp_path, matcher=SecretMatcher({"X": b"other-v4lue"}))
    assert clean is not None and leak in clean.params_json
