"""V0.9 G1 tester round 2: attacks on the fix-round-1 code (pure logic).

Names starting ``test_defect_`` are strict xfails: they state the correct
behaviour and fail today (see the tester r2 report in the progress file).
"""

from __future__ import annotations

import datetime as dt
import json
import random
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.execution.workload_result import (
    MAX_COMPUTE_ITERATIONS,
    read_workload_result,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.secret_redaction import SecretMatcher
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.session.clocks import (
    ALERT_BOUND_MS,
    WINDOW_AGE_S,
    WINDOW_EXCHANGES,
    NodeClock,
)
from meshforge.controller.telemetry.record import RecordSource, build_record

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.UTC)
WALL0 = 1_760_000_000.0


# ------------------------------------------------------------------ clock


def _feed(clock: NodeClock, t_ms: float, res_ms: float) -> None:
    clock.observe(
        sent_wall_ms=WALL0 * 1000 + t_ms,
        rtt_ms=2.0,
        agent_ms=WALL0 * 1000 + t_ms + 1.0,
        agent_resolution_ms=res_ms,
    )


def test_a_coarse_resolution_ages_out_by_time_not_only_by_count() -> None:
    clock = NodeClock()
    _feed(clock, 0.0, 600.0)
    for i in range(1, 6):
        _feed(clock, i * 1000.0, 0.001)
    assert clock.uncertain  # the coarse exchange is in the window
    # one ping after the age horizon: the old ones leave the window by age
    for i in range(3):
        _feed(clock, (WINDOW_AGE_S + 10) * 1000.0 + i * 1000.0, 0.001)
    assert len(clock.resolutions) == len(clock.window)
    assert max(clock.resolutions) < 1.0
    assert clock.latest is not None and clock.latest.bound_ms < ALERT_BOUND_MS


def test_the_resolutions_stay_in_step_with_the_window_under_random_feeds() -> None:
    rng = random.Random(7)  # noqa: S311 - deterministic test feed
    clock = NodeClock()
    t = 0.0
    for _ in range(600):
        t += rng.choice([0.5, 1000.0, 6000.0, 400_000.0, -50_000.0])  # steps back too
        _feed(clock, t, rng.choice([0.001, 1.0, 15.6, 600.0]))
        assert len(clock.resolutions) == len(clock.window) <= WINDOW_EXCHANGES
        assert len(clock.resolutions) >= 1


def test_a_local_clock_that_goes_back_drops_the_old_coarse_report() -> None:
    clock = NodeClock()
    _feed(clock, 100_000.0, 600.0)
    _feed(clock, 101_000.0, 0.001)
    _feed(clock, 5_000.0, 0.001)  # earlier than the newest: window cleared
    assert len(clock.window) == len(clock.resolutions) == 1
    assert max(clock.resolutions) == 0.001


def test_non_finite_and_negative_exchanges_do_not_desync_the_resolutions() -> None:
    clock = NodeClock()
    _feed(clock, 0.0, 1.0)
    clock.observe(
        sent_wall_ms=float("nan"), rtt_ms=1.0, agent_ms=1.0, agent_resolution_ms=900.0
    )
    clock.observe(
        sent_wall_ms=5.0, rtt_ms=-1.0, agent_ms=1.0, agent_resolution_ms=900.0
    )
    assert len(clock.resolutions) == len(clock.window) == 1
    assert max(clock.resolutions) == 1.0


# ------------------------------------------------- controller report parse


def _status(report: agent_pb2.WorkloadReport, compute_ns: int | None = 10) -> Any:
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


@pytest.mark.parametrize("iterations", [1, 2, MAX_COMPUTE_ITERATIONS])
def test_iterations_inside_the_bounds_are_accepted_through_proto(
    iterations: int,
) -> None:
    report = agent_pb2.WorkloadReport(family="cpu.x", status="ok")
    report.compute_iterations = iterations
    parsed = parse_status(_status(report))
    assert not isinstance(parsed, str), parsed
    assert parsed.workload is not None
    assert parsed.workload["compute_iterations"] == iterations


@pytest.mark.parametrize("iterations", [0, MAX_COMPUTE_ITERATIONS + 1, 2**32 - 1])
def test_iterations_outside_the_bounds_are_refused_through_proto(
    iterations: int,
) -> None:
    report = agent_pb2.WorkloadReport(family="cpu.x", status="ok")
    report.compute_iterations = iterations
    assert isinstance(parse_status(_status(report)), str)


def test_an_old_agent_without_the_field_still_yields_a_record() -> None:
    report = agent_pb2.WorkloadReport(family="cpu.matmul", status="ok", device="cpu")
    wire = report.SerializeToString()  # field 7 never set: what an old agent sends
    assert b"\x38" not in wire[-3:] or not report.HasField("compute_iterations")
    parsed = parse_status(_status(agent_pb2.WorkloadReport.FromString(wire)))
    assert not isinstance(parsed, str), parsed
    assert parsed.workload is not None
    assert parsed.workload["compute_iterations"] is None
    record = build_record(_source(parsed.workload))
    assert record["compute_ns"] == 10 and record["compute_iterations"] is None
    assert record["failure_class"] == "ok"


def test_an_old_agent_with_a_params_less_report_still_yields_a_record() -> None:
    parsed = parse_status(_status(agent_pb2.WorkloadReport(status="unavailable"), None))
    assert not isinstance(parsed, str), parsed
    record = build_record(_source(parsed.workload))
    assert record["compute_ns"] is None and record["compute_iterations"] is None


def _source(workload: Mapping[str, Any] | None) -> Any:
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


@pytest.mark.parametrize(
    "params_json", ['{"a":1e400}', '{"a":-1e999}', '{"a":[1e999]}']
)
@pytest.mark.xfail(
    strict=True,
    reason="T2-1: the controller accepts +-Infinity in params_json (1e400 is not a "
    "'constant' to json.loads); JSONB refuses 'Infinity', so the attempt's terminal "
    "write would fail",
)
def test_defect_t2_1_infinite_numbers_in_params_are_refused(params_json: str) -> None:
    report = agent_pb2.WorkloadReport(
        family="cpu.x", status="ok", params_json=params_json
    )
    parsed = parse_status(_status(report))
    if isinstance(parsed, str):
        return
    assert parsed.workload is not None
    # whatever is kept must be JSON a PostgreSQL JSONB can hold
    json.loads(json.dumps(parsed.workload["params"], allow_nan=False))


def test_deeply_nested_params_are_not_a_crash_and_not_a_record_value() -> None:
    deep = "[" * 900 + "]" * 900
    report = agent_pb2.WorkloadReport(
        family="cpu.x", status="ok", params_json='{"a":' + deep + "}"
    )
    parsed = parse_status(_status(report))
    if isinstance(parsed, str):
        return
    record = build_record(_source(parsed.workload))
    assert record["workload_params"] is None and record["workload_status"] == "ok"


# --------------------------------------------------------- record builder


def test_every_short_column_is_null_not_cut_when_over_64() -> None:
    long = "x" * 65
    record = build_record(
        RecordSource(
            attempt_id="att_1",
            job_id="job_1",
            attempt_no=1,
            node_id="node_b",
            state="done",
            created_at=T0,
            ended_at=T0 + dt.timedelta(seconds=3),
            exit_code=0,
            runtime=long,
            manifest={"env": {"base": long}, "resources": {"gpu": long}},
            workload={"family": long, "status": long, "device": long, "params": {}},
            campaign={"campaign_id": long, "group_id": long, "config_id": long},
            start_snapshot={"node": {"state": long}, "network": {"source": long}},
        )
    )
    for column in (
        "runtime",
        "env_base",
        "gpu_request",
        "workload_family",
        "workload_status",
        "workload_device",
        "campaign_id",
        "group_id",
        "config_id",
        "node_state",
        "net_source",
    ):
        assert record[column] is None, column


def test_the_unicode_length_is_counted_in_characters_like_varchar() -> None:
    name = "é" * 64  # 128 bytes, 64 characters: VARCHAR(64) holds it
    record = build_record(_source({"family": name, "status": "ok", "params": {}}))
    assert record["workload_family"] == name


def test_no_value_of_a_built_record_has_a_nul_or_lone_surrogate() -> None:
    record = build_record(
        _source(
            {
                "family": "a\x00b",
                "status": "ok",
                "device": "\ud800",
                "params": {"dtype": "x\x00", "k": [{"\x00": 1}]},
            }
        )
    )

    def walk(v: object) -> None:
        if isinstance(v, str):
            assert "\x00" not in v
            v.encode("utf-8")
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(k)
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)

    for value in record.values():
        walk(value)


# ----------------------------------------------------------- secret matching


def _write(workdir: Path, params: object) -> None:
    target = workdir / "results" / "workload.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "family": "cpu.x",
                "params": params,
                "status": "ok",
                "compute_ns": 5,
                "compute_iterations": 2,
            }
        ),
        encoding="utf-8",
    )


SECRET = "pässwörd-日本-s3cret!"


@pytest.mark.security
@pytest.mark.parametrize(
    "params",
    [
        {"k": [SECRET]},
        {"k": [[[SECRET]]]},
        {"k": {"deep": {"deeper": [SECRET]}}},
        {SECRET: 1},
        {"a": {SECRET: [1]}},
        {"k": f"prefix {SECRET} suffix"},
        {"k": [1, 2, {"z": SECRET}]},
    ],
    ids=[
        "list",
        "nested-list",
        "nested-dict",
        "key",
        "nested-key",
        "embedded",
        "mixed",
    ],
)
def test_a_secret_anywhere_in_the_decoded_params_drops_them(
    tmp_path: Path, params: object
) -> None:
    _write(tmp_path, params)
    matcher = SecretMatcher({"S": SECRET.encode()})
    report = read_workload_result(tmp_path, matcher=matcher)
    assert report is not None and report.params_json == ""
    assert report.status == "ok" and report.compute_iterations == 2
    assert SECRET not in str(report)


@pytest.mark.security
def test_a_matcher_with_no_secret_changes_nothing(tmp_path: Path) -> None:
    _write(tmp_path, {"k": [SECRET]})
    plain = read_workload_result(tmp_path)
    assert plain is not None and plain.params_json != ""
    empty = read_workload_result(tmp_path, matcher=SecretMatcher({}))
    assert empty == plain


@pytest.mark.security
def test_a_one_million_nodes_params_tree_does_not_stall_or_recurse(
    tmp_path: Path,
) -> None:
    import time

    # 4096-char cap on params_json: large nesting is dropped, never a stall
    params: object = {"k": ["x"] * 800}
    _write(tmp_path, params)
    started = time.monotonic()
    report = read_workload_result(
        tmp_path, matcher=SecretMatcher({"S": b"not-there-123"})
    )
    assert time.monotonic() - started < 5
    assert report is not None


@pytest.mark.security
def test_a_matcher_stall_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.agent.execution import workload_result as wr
    from meshforge.common.secret_redaction import RedactionStallError

    def stall(data: bytes, matcher: object) -> bytes:
        raise RedactionStallError("stalled")

    monkeypatch.setattr(wr, "redact_bytes", stall)
    _write(tmp_path, {"k": "v"})
    report = read_workload_result(tmp_path, matcher=SecretMatcher({"S": b"abcdefgh"}))
    assert report is not None and report.params_json == "" and report.family == ""


@pytest.mark.security
def test_unicode_normalisation_variants_are_a_documented_residual(
    tmp_path: Path,
) -> None:
    """A workload that knows the secret can re-encode it (NFD, base64, split):
    the matcher is exact-bytes by design (RE-I6 residual).  This test pins the
    current behaviour so a change is noticed; it is not a defect."""
    secret = "café-secret-value-1"
    nfd = unicodedata.normalize("NFD", secret)
    assert nfd != secret
    _write(tmp_path, {"k": nfd})
    report = read_workload_result(
        tmp_path, matcher=SecretMatcher({"S": secret.encode()})
    )
    assert report is not None
    # pinned: NFD variant is NOT matched today
    assert report.params_json != ""
