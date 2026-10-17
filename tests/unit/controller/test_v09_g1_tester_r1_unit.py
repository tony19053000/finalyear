"""V0.9 G1 tester round 1 (pure logic): try to break the clock estimate, the
failure class, the record builder, the Parquet export determinism and the
workload report validation.

Tests named ``test_DEFECT_*`` are strict xfails: each documents a defect found
by the tester (see the tester report); when the implementer fixes it the xfail
turns into an XPASS failure, which is the signal to remove the marker.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.session.clocks import (
    MAX_RESOLUTION_MS,
    UNKNOWN_RESOLUTION_MS,
    NodeClock,
    resolution_ms,
)
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.record import (
    PhaseValue,
    RecordSource,
    build_record,
    failure_class,
)
from meshforge.controller.telemetry.schema import COLUMNS, FailureClass, Kind

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.UTC)
WALL = 1_792_238_400_000.0  # ms


def _source(**overrides: Any) -> RecordSource:
    values: dict[str, Any] = {
        "attempt_id": "att_1",
        "job_id": "job_1",
        "attempt_no": 1,
        "node_id": "node_b",
        "state": "failed",
        "created_at": T0,
        "ended_at": T0 + dt.timedelta(seconds=9),
        "exit_code": 1,
        "failure_type": "user_error",
        "manifest": {},
        "placement": {"node_id": "node_b"},
        "phases": {"run": PhaseValue(10, False)},
    }
    values.update(overrides)
    return RecordSource(**values)


# ------------------------------------------------------------------- clocks


def _feed(
    clock: NodeClock,
    n: int,
    *,
    start: int = 0,
    offset_ms: float = 0.0,
    rtt_ms: float = 2.0,
    res_ms: float = 0.001,
    period_ms: float = 6000.0,
) -> list[Any]:
    changes = []
    for i in range(start, start + n):
        sent = WALL + i * period_ms
        change = clock.observe(
            sent_wall_ms=sent,
            rtt_ms=rtt_ms,
            agent_ms=sent + rtt_ms / 2 + offset_ms,
            agent_resolution_ms=res_ms,
        )
        if change is not None:
            changes.append(change)
    return changes


def test_a_stable_clock_raises_no_alert_over_hours() -> None:
    clock = NodeClock()
    assert _feed(clock, 2000) == []
    assert clock.latest is not None and clock.latest.consistent


def test_a_clock_step_alerts_once_and_recovers_once_as_the_window_ages() -> None:
    clock = NodeClock()
    _feed(clock, 40)
    changes = _feed(clock, 80, start=40, offset_ms=10_000.0)  # agent stepped +10 s
    flips = [c.uncertain for c in changes]
    # exactly one episode: into uncertain once, back to certain once (after the
    # old exchanges aged out of the 32-exchange window)
    assert flips == [True, False], flips
    assert clock.latest is not None and abs(clock.latest.offset_ms - 10_000) < 5


def test_a_reconnect_starts_a_fresh_estimate_and_does_not_alert_early() -> None:
    first = NodeClock()
    _feed(first, 20, offset_ms=5000.0)
    reconnect = NodeClock()  # a new session = a new LiveSession.clock
    assert _feed(reconnect, 3, offset_ms=-9000.0) == []  # < MIN_EXCHANGES
    assert reconnect.latest is not None
    assert abs(reconnect.latest.offset_ms + 9000) < 5


def test_agent_wall_clock_going_backwards_is_not_a_crash() -> None:
    clock = NodeClock()
    _feed(clock, 10)
    # controller wall clock stepped back 1 h between two Pings
    changes = _feed(clock, 10, start=-600)
    assert all(isinstance(c.uncertain, bool) for c in changes)
    assert clock.latest is not None and math.isfinite(clock.latest.offset_ms)


@pytest.mark.parametrize(
    "agent_ms", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"]
)
def test_non_finite_agent_stamps_are_rejected_and_counted(agent_ms: float) -> None:
    clock = NodeClock()
    assert (
        clock.observe(
            sent_wall_ms=WALL, rtt_ms=2.0, agent_ms=agent_ms, agent_resolution_ms=1.0
        )
        is None
    )
    assert clock.latest is None and clock.exchanges_rejected == 1


def test_a_negative_rtt_is_rejected() -> None:
    clock = NodeClock()
    clock.observe(sent_wall_ms=WALL, rtt_ms=-1.0, agent_ms=WALL, agent_resolution_ms=1)
    assert clock.latest is None and clock.exchanges_rejected == 1


def test_resolution_mapping_unknown_is_16ms_and_hostile_values_are_clamped() -> None:
    assert resolution_ms(0) == UNKNOWN_RESOLUTION_MS == 16.0
    assert resolution_ms(-5) == UNKNOWN_RESOLUTION_MS
    assert resolution_ms(1) == 0.001
    assert resolution_ms(2**63) == MAX_RESOLUTION_MS


def test_an_old_agent_without_resolution_gets_a_wider_bound_than_a_precise_one() -> None:
    old, precise = NodeClock(), NodeClock()
    _feed(old, 6, res_ms=resolution_ms(0))
    _feed(precise, 6, res_ms=resolution_ms(1))
    assert old.latest is not None and precise.latest is not None
    assert old.latest.bound_ms > precise.latest.bound_ms
    assert old.latest.bound_ms >= UNKNOWN_RESOLUTION_MS / 2 - 1e-9


def test_a_drift_of_the_agent_clock_inside_the_bound_is_not_hidden() -> None:
    """An offset that moves 40 ms over the window must show as inconsistent or
    widen the bound; the estimate may not report a tight stale value."""
    clock = NodeClock()
    for i in range(32):
        _feed(clock, 1, start=i, offset_ms=i * 40.0 / 31, rtt_ms=2.0)
    assert clock.latest is not None
    assert (not clock.latest.consistent) or clock.latest.bound_ms >= 19.0


@pytest.mark.xfail(strict=True, reason="DEFECT-1: bound uses the latest exchange's resolution only")
def test_DEFECT_1_alternating_reported_resolution_flaps_the_alert_every_ping() -> None:
    """The bound uses only the *latest* exchange's resolution, so an agent (or
    any clock whose resolution report varies) toggles the node between
    uncertain/certain on every Ping: one audit_event row + one live alert per
    flip, i.e. the "one alert per episode" rule does not hold."""
    clock = NodeClock()
    flips = 0
    for i in range(40):
        res = 1000.0 if i % 2 else 0.001
        flips += len(_feed(clock, 1, start=i, res_ms=res, rtt_ms=2.0))
    assert flips <= 4, f"{flips} alert transitions in 40 identical-quality exchanges"


# ------------------------------------------------------------ failure class


@pytest.mark.parametrize(
    ("state", "ftype", "exit_status", "workload", "expected"),
    [
        ("done", None, 0, None, FailureClass.OK),
        ("done", None, 0, {"status": "ok"}, FailureClass.OK),
        ("failed", "user_error", 3, {"status": "unavailable"}, FailureClass.UNAVAILABLE),
        ("failed", "user_error", 3, {"status": "ok"}, FailureClass.UNAVAILABLE),
        ("failed", "user_error", 4, {"status": "check_failed"}, FailureClass.WORKLOAD),
        ("failed", "user_error", 4, {"status": "ok"}, FailureClass.WORKLOAD),
        ("failed", "user_error", 2, {"status": "bad_params"}, FailureClass.WORKLOAD),
        ("failed", "user_error", 1, None, FailureClass.WORKLOAD),
        ("failed", "user_error", 3, None, FailureClass.WORKLOAD),  # not a benchmark
        ("failed", "oom", 137, None, FailureClass.WORKLOAD),
        ("failed", "vram_oom", None, None, FailureClass.WORKLOAD),
        ("failed", "deadline_exceeded", None, None, FailureClass.WORKLOAD),
        ("failed", "cancelled", None, None, FailureClass.UNAVAILABLE),
        ("failed", "node_lost", None, None, FailureClass.INFRASTRUCTURE),
        ("lost", "node_lost", None, None, FailureClass.INFRASTRUCTURE),
        ("fenced", None, None, None, FailureClass.INFRASTRUCTURE),
        ("failed", None, None, None, FailureClass.INFRASTRUCTURE),
        ("failed", "no-such-type", 1, None, FailureClass.INFRASTRUCTURE),
        ("failed", "network_error", 1, None, FailureClass.INFRASTRUCTURE),
        ("failed", "disk_full", None, None, FailureClass.INFRASTRUCTURE),
        ("failed", "internal_error", 1, {"status": "ok"}, FailureClass.INFRASTRUCTURE),
    ],
)
def test_failure_class_matrix(
    state: str,
    ftype: str | None,
    exit_status: int | None,
    workload: dict[str, Any] | None,
    expected: FailureClass,
) -> None:
    if ftype in {t.value for t in FailureType} or ftype in (None, "no-such-type"):
        assert failure_class(state, ftype, exit_status, workload) is expected


def test_every_failure_type_and_state_maps_to_a_declared_class() -> None:
    declared = {c.value for c in FailureClass}
    for state in ("done", "failed", "lost", "fenced"):
        for ftype in [*(t.value for t in FailureType), None, "junk"]:
            for workload in (None, {"status": "ok"}, {"status": "unavailable"}):
                for exit_status in (None, 0, 1, 3, 4, 137):
                    cls = failure_class(state, ftype, exit_status, workload)
                    assert cls.value in declared
                    if state == "done":
                        assert cls is FailureClass.OK


def test_infrastructure_failures_never_count_as_workload_failures() -> None:
    """An infra fault on a benchmark must not be labelled as the workload's
    own failure (that would poison the dataset's failure statistics)."""
    for ftype in ("node_lost", "network_error", "disk_full", "env_error"):
        if ftype not in {t.value for t in FailureType}:
            continue
        assert (
            failure_class("failed", ftype, 1, {"status": "ok"})
            is FailureClass.INFRASTRUCTURE
        )


# ----------------------------------------------------------- record builder

JSON_KINDS = {c.name for c in COLUMNS if c.kind is Kind.JSON}
HOSTILE = [
    "x" * 200,
    "a\x00b",
    "\ud800",
    "d" * 65,
    "\x00",
]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


@pytest.mark.xfail(strict=True, reason="DEFECT-2: untrusted params reach VARCHAR(64)/JSONB unsanitised")
@pytest.mark.parametrize("bad", HOSTILE, ids=["200chars", "nul", "surrogate", "65", "nul-only"])
def test_DEFECT_2_hostile_workload_params_must_fit_postgres_columns(bad: str) -> None:
    """A workload (it is untrusted) chooses ``params``; ``p_dtype`` becomes a
    ``VARCHAR(64)`` column and the JSON goes into JSONB (no NUL, no lone
    surrogate). A value that Postgres refuses makes the *whole sweep batch*
    fail forever (every later attempt loses its record) - the builder must
    bound/sanitise, or reject the single record."""
    record = build_record(
        _source(
            workload={
                "family": "cpu.matmul",
                "params": {"dtype": bad, "note": bad, bad: 1},
                "status": "ok",
            }
        )
    )
    lengths = {
        c.name: c.type.length
        for c in RunRecord.__table__.columns
        if getattr(c.type, "length", None)
    }
    for name, value in record.items():
        if isinstance(value, str) and name in lengths:
            assert len(value) <= lengths[name], name
        for text in _strings(value) if name in JSON_KINDS else [value] if isinstance(value, str) else []:
            assert "\x00" not in text, name
            text.encode("utf-8")  # a lone surrogate cannot be stored


def test_hostile_snapshot_numbers_never_leak_non_finite_or_oversized_values() -> None:
    snapshot = {
        "node": {"state": 7, "rtt_ms": math.inf},
        "resources": {
            "cpu_percent": math.nan,
            "ram_used_bytes": 2**80,
            "ram_total_bytes": True,
            "gpus": [{"index": 0, "util_percent": -math.inf}],
            "age_s": "3",
        },
        "gpu_indices": [0],
        "scores": {"metrics": "nope"},
        "telemetry": {
            "node_live_attempts": 2**70,
            "cache": [{"bytes": math.nan, "warm": "yes"}, 5, None],
            "network": {"rtt_ms": math.nan, "samples": 1.5},
            "clock": {"clock_offset_ms": math.inf, "consistent": 1},
        },
    }
    record = build_record(_source(start_snapshot=snapshot))
    for name, value in record.items():
        if isinstance(value, float):
            assert math.isfinite(value), name
        if isinstance(value, int) and not isinstance(value, bool):
            assert abs(value) < 2**63, name
    json.dumps(
        {k: record[k] for k in JSON_KINDS}, allow_nan=False
    )  # JSONB-safe (no NaN)
    assert record["clock_consistent"] is None


def test_a_record_survives_a_start_snapshot_of_the_wrong_shape() -> None:
    for snapshot in (None, {}, {"telemetry": "x"}, {"node": []}, {"resources": "x"}):
        record = build_record(_source(start_snapshot=snapshot))  # type: ignore[arg-type]
        assert record["attempt_id"] == "att_1"


def test_pinned_jobs_have_no_planner_node_and_unpinned_keep_the_choice() -> None:
    pinned = build_record(_source(decision_chosen="node_b"))
    auto = build_record(_source(placement={}, decision_chosen="node_b"))
    assert pinned["planner_node_id"] is None and pinned["placement_mode"] == "pinned"
    assert auto["planner_node_id"] == "node_b" and auto["placement_mode"] == "auto"


def test_compute_phase_is_the_workloads_own_time_not_added_to_total() -> None:
    record = build_record(
        _source(
            state="done",
            workload={"status": "ok", "compute_ns": 5_000_000_000},
            phases={
                "queue": PhaseValue(10, False),
                "run": PhaseValue(6000, False),
                "compute": PhaseValue(5000, False),
            },
        )
    )
    assert record["compute_ns"] == 5_000_000_000
    assert record["total_ms"] == 6010  # queue + run; compute is inside run
    negative = build_record(
        _source(workload={"status": "ok", "compute_ns": -5}, state="done")
    )
    assert negative["compute_ns"] is None


def test_records_are_a_pure_function_of_the_source() -> None:
    a, b = build_record(_source()), build_record(_source())
    assert a == b


# --------------------------------------------------------- export determinism


_SCRIPT = textwrap.dedent(
    """
    import datetime as dt, hashlib, sys
    from meshforge.controller.telemetry.export import parquet_bytes
    from meshforge.controller.telemetry.record import build_record, RecordSource, PhaseValue
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    rows = []
    for i in range(30):
        rec = build_record(RecordSource(
            attempt_id=f"att_{i:03d}", job_id=f"job_{i}", attempt_no=1,
            node_id="nä", state="done", created_at=dt.datetime(2026, 10, 17, 9, 30, tzinfo=tz),
            ended_at=dt.datetime(2026, 10, 17, 9, 31, tzinfo=tz), exit_code=0,
            workload={"family": "cpu.matmul", "status": "ok", "compute_ns": 10 + i,
                      "params": {"z": 1, "a": "ünï", "n": 512 + i, "f": 1e22, "t": 0.1 + i}},
            placement={}, manifest={},
            phases={"run": PhaseValue(100 + i, False)},
            campaign={"campaign_id": None if i % 3 == 0 else "cé", "config_id": f"cfg_{i % 4}", "repetition": i % 5},
        ))
        rec["recorded_at"] = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.UTC)
        rows.append(rec)
    if len(sys.argv) > 1 and sys.argv[1] == "reverse":
        rows.reverse()
    sys.stdout.write(hashlib.sha256(parquet_bytes(rows)).hexdigest())
    """
)


def _digest(env_extra: dict[str, str], *args: str) -> str:
    env = {**os.environ, **env_extra}
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT, *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


def test_export_bytes_do_not_depend_on_process_tz_locale_hashseed_or_input_order() -> None:
    runs = {
        _digest({"TZ": "UTC", "LC_ALL": "C", "PYTHONHASHSEED": "0"}),
        _digest({"TZ": "Asia/Kolkata", "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "1"}),
        _digest({"TZ": "America/St_Johns", "LANG": "tr_TR.UTF-8", "PYTHONHASHSEED": "12345"}),
        _digest({"TZ": "Pacific/Kiritimati", "PYTHONHASHSEED": "random"}, "reverse"),
        _digest({"TZ": "Pacific/Auckland", "PYTHONHASHSEED": "random"}, "reverse"),
    }
    assert len(runs) == 1, runs


# ------------------------------------------- parse_status (controller side)


def _terminal_status(**workload: Any) -> agent_pb2.AttemptStatus:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE,
    )
    status.phases.run_s = 1.0
    status.workload.CopyFrom(agent_pb2.WorkloadReport(status="ok", **workload))
    return status


@pytest.mark.parametrize(
    "params_json",
    [
        "NaN",
        '{"a": NaN}',
        '{"a": Infinity}',
        "[1,2]",
        "{" * 3000,
        '{"a":' * 900 + "1" + "}" * 900,
        "x" * 5000,
        '{"k": "\\u0000"}',
    ],
)
def test_parse_status_never_raises_on_hostile_workload_params(params_json: str) -> None:
    """Refused with a message or accepted - never an exception (an exception in
    the report handler would drop the terminal report)."""
    result = parse_status(_terminal_status(params_json=params_json))
    assert isinstance(result, str) or result.workload is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"family": "UPPER"},
        {"family": "a" * 65},
        {"device": "cuda:0 ; drop"},
        {"status": "bogus"},
    ],
)
def test_parse_status_refuses_unbounded_names_and_unknown_status(
    fields: dict[str, Any],
) -> None:
    status = _terminal_status()
    for key, value in fields.items():
        setattr(status.workload, key, value)
    assert isinstance(parse_status(status), str)


def test_a_report_from_an_old_agent_without_workload_or_compute_parses() -> None:
    """Backward compatibility: no WorkloadReport, no compute_ns."""
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE,
    )
    status.phases.run_s = 1.0
    result = parse_status(status)
    assert not isinstance(result, str), result
    assert result.workload is None
