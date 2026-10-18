"""V0.9 G4 lane B: the controller side of benchmark conditions (D217/D241), pure parts.

The agent's ``ConditionReport`` parse, the record's condition outcome and
failure class, the filler / warm-up keys and the node pauses. The two record
columns (``condition``, ``condition_measured``) come with lane A's schema v2;
until then a test stub adds them to the column list to prove the fill.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.bench_conditions import parse_condition
from meshforge.common.ids import new_bench_run_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.bench.conditions import (
    NodePauses,
    filler_key,
    run_id_of_key,
    warmup_key,
)
from meshforge.controller.bench.submit import _pre_jobs
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.telemetry import record as record_module
from meshforge.controller.telemetry.conditions import condition_outcome
from meshforge.controller.telemetry.record import RecordSource, build_record
from meshforge.controller.telemetry.schema import FailureClass

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)


# ------------------------------------------------------------ report parse


def _status(
    *,
    spec: str = "cpu_load:2",
    verified: bool = True,
    measured: str = '{"cores":2,"load_cores":1.97}',
    reason: str = "",
    state: int = common_pb2.ATTEMPT_STATE_DONE,
) -> agent_pb2.AttemptStatus:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=state,
    )
    if state == common_pb2.ATTEMPT_STATE_FAILED:
        status.failure.CopyFrom(
            common_pb2.Failure(
                failure_type=common_pb2.FAILURE_TYPE_CONFIG_ERROR,
                message="condition_failed:cpu_load: too busy",
            )
        )
    status.condition.CopyFrom(
        agent_pb2.ConditionReport(
            spec=spec,
            verified=verified,
            measured_json=measured,
            reason=reason,
            reverted=True,
        )
    )
    return status


def test_condition_report_lands_in_the_workload_document() -> None:
    parsed = parse_status(_status())
    assert not isinstance(parsed, str), parsed
    assert parsed.workload == {
        "condition": {
            "spec": "cpu_load:2",
            "verified": True,
            "measured": {"cores": 2, "load_cores": 1.97},
            "reason": None,
            "reverted": True,
        }
    }


def test_failed_condition_report_with_its_reason() -> None:
    parsed = parse_status(
        _status(
            verified=False,
            reason="measured 0.4 busy cores",
            state=common_pb2.ATTEMPT_STATE_FAILED,
        )
    )
    assert not isinstance(parsed, str), parsed
    assert parsed.workload is not None
    assert parsed.workload["condition"]["verified"] is False
    assert parsed.workload["condition"]["reason"] == "measured 0.4 busy cores"


def test_workload_and_condition_reports_share_the_document() -> None:
    status = _status()
    status.phases.run_s = 1.0
    status.phases.compute_ns = 10
    status.workload.CopyFrom(agent_pb2.WorkloadReport(family="cpu.sort", status="ok"))
    parsed = parse_status(status)
    assert not isinstance(parsed, str), parsed
    assert parsed.workload is not None
    assert parsed.workload["family"] == "cpu.sort"
    assert parsed.workload["condition"]["spec"] == "cpu_load:2"


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        ({"spec": "cpu_load"}, "not a condition"),
        ({"spec": "netem:loss=1.0%"}, "not canonical"),
        ({"measured": "x" * 2049}, "over 2048"),
        ({"measured": "[1]"}, "not a JSON object"),
        ({"measured": '{"a": NaN}'}, "not JSON"),
        ({"measured": '{"a": 1e400}'}, "not JSON"),
        ({"measured": '{"a": "\\u0000"}'}, "NUL"),
        ({"reason": "x" * 513}, "maximum is 512"),
        ({"reason": "bad\x07bell", "verified": False}, "non-printable"),
        ({"reason": "why"}, "verified with a reason"),
        ({"state": common_pb2.ATTEMPT_STATE_RUNNING}, "non-terminal"),
    ],
)
def test_bad_condition_reports_are_refused_never_cleaned(
    changes: dict[str, Any], needle: str
) -> None:
    parsed = parse_status(_status(**changes))
    assert isinstance(parsed, str) and needle in parsed, parsed


# ------------------------------------------------------------ record outcome


def _identity(condition: str, **facts: Any) -> dict[str, Any]:
    return {
        "campaign_id": "camp_1",
        "group_id": "grp_1",
        "config_id": "cfg_1",
        "repetition": 1,
        "run_id": "brun_1",
        "condition": condition,
        "session_label": "wifi",
        **({"condition_facts": facts} if facts else {}),
    }


def _report(spec: str, verified: bool = True, **measured: Any) -> dict[str, Any]:
    return {
        "condition": {
            "spec": spec,
            "verified": verified,
            "measured": measured or None,
            "reason": None if verified else "too busy",
            "reverted": True,
        }
    }


@pytest.mark.parametrize(
    ("campaign", "workload", "state", "verified", "failed"),
    [
        (None, None, "done", None, False),  # not a campaign run
        (_identity("none"), None, "done", True, False),
        (
            _identity("cpu_load:1"),
            _report("cpu_load:1", load_cores=0.97),
            "done",
            True,
            False,
        ),
        (_identity("cpu_load:1"), _report("cpu_load:1", False), "failed", False, True),
        # the agent's load died while the job ran: the job finished, not labelled
        (_identity("cpu_load:1"), _report("cpu_load:1", False), "done", False, True),
        (_identity("cpu_load:1"), None, "done", False, True),  # no report at all
        (_identity("cpu_load:1"), None, "lost", False, False),  # lost: keeps infra
        (_identity("cpu_load:1"), _report("cpu_load:2"), "done", False, True),
        (
            _identity("cache:cold"),
            _report("cache:cold", blobs_hit=0),
            "done",
            True,
            False,
        ),
        (_identity("queue:2", fillers=2, fillers_ahead=2), None, "done", True, False),
        (_identity("queue:2", fillers=2, fillers_ahead=1), None, "done", False, True),
        (
            _identity("queue:2", fillers=2, fillers_ahead=1),
            None,
            "failed",
            False,
            False,
        ),
        (_identity("queue:2"), None, "done", False, True),
        (
            _identity("unavailable:30s", dispatched_after_s=2.5),
            None,
            "done",
            True,
            False,
        ),
        # a controller restart during the pause (it lives in memory)
        (
            _identity("unavailable:30s", dispatched_after_s=2.5, pause_lost=True),
            None,
            "done",
            False,
            True,
        ),
        (
            _identity("unavailable:30s", dispatched_after_s=31.0),
            None,
            "done",
            False,
            True,
        ),
        (
            _identity("unavailable:30s", dispatched_after_s=None),
            None,
            "done",
            False,
            True,
        ),
        (_identity("bogus:1"), None, "done", False, True),
    ],
)
def test_condition_outcome(
    campaign: dict[str, Any] | None,
    workload: dict[str, Any] | None,
    state: str,
    verified: bool | None,
    failed: bool,
) -> None:
    outcome = condition_outcome(campaign=campaign, workload=workload, state=state)
    assert outcome.failed is failed
    if verified is None:
        assert outcome.condition is None and outcome.measured is None
        return
    assert outcome.measured is not None
    assert outcome.measured["verified"] is verified
    assert outcome.measured["session_label"] == "wifi"
    json.dumps(outcome.measured, allow_nan=False)  # storable as JSONB
    if not verified:
        assert outcome.measured["reason"]


def test_outcome_carries_agent_and_controller_measurements() -> None:
    outcome = condition_outcome(
        campaign=_identity("cache:warm", warmup_state="succeeded"),
        workload=_report("cache:warm", blobs_hit=3, blobs_fetched=0),
        state="done",
    )
    assert outcome.condition == "cache:warm"
    assert outcome.measured == {
        "verified": True,
        "reason": None,
        "agent": {"blobs_hit": 3, "blobs_fetched": 0},
        "reverted": True,
        "controller": {"warmup_state": "succeeded"},
        "session_label": "wifi",
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
        "placement": {"mode": "local", "node_id": "node_b"},
        "submitter_node_id": "node_b",
        "campaign": _identity("cpu_load:1"),
        "workload": {
            "family": "cpu.sort",
            "params": {"n": 1000},
            "status": "ok",
            "compute_ns": 5,
            **_report("cpu_load:1", load_cores=1.0),
        },
    }
    values.update(overrides)
    return RecordSource(**values)


def test_record_class_is_condition_failed_when_the_condition_did_not_hold() -> None:
    ok = build_record(_source())
    assert ok["failure_class"] == FailureClass.OK.value
    broken = _source(workload={**_source().workload, **_report("cpu_load:1", False)})
    assert build_record(broken)["failure_class"] == FailureClass.CONDITION_FAILED.value
    refused = _source(
        state="failed",
        failure_type="config_error",
        exit_code=None,
        workload=_report("cpu_load:1", False),
    )
    record = build_record(refused)
    assert record["failure_class"] == FailureClass.CONDITION_FAILED.value
    assert record["failure_type"] == "config_error"
    # an ordinary job (no campaign) is untouched
    plain = build_record(_source(campaign=None, workload=None))
    assert plain["failure_class"] == FailureClass.OK.value


def test_record_fills_the_v2_condition_columns_once_the_schema_has_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub of lane A's schema v2 (migration 0023): the columns exist ->
    build_record fills them; before the merge they are absent."""
    assert (
        "condition" not in build_record(_source())
        or record_module._HAS_CONDITION_COLUMNS
    )
    monkeypatch.setattr(
        record_module,
        "COLUMN_NAMES",
        (*record_module.COLUMN_NAMES, "condition", "condition_measured"),
    )
    monkeypatch.setattr(record_module, "_HAS_CONDITION_COLUMNS", True)
    record = build_record(_source())
    assert record["condition"] == "cpu_load:1"
    assert record["condition_measured"]["verified"] is True
    assert record["condition_measured"]["agent"] == {"load_cores": 1.0}
    plain = build_record(_source(campaign=None, workload=None))
    assert plain["condition"] is None and plain["condition_measured"] is None


# ------------------------------------------------------------ keys + pauses


def test_keys() -> None:
    run_id = new_bench_run_id()
    assert run_id_of_key(f"bench:{run_id}") == run_id
    assert run_id_of_key(filler_key(run_id, 1)) is None
    assert run_id_of_key(warmup_key(run_id)) is None
    assert filler_key(run_id, 2).startswith("bench:")  # exempt from aging (D237)
    for key in [None, "", "bench:", "bench:job_x", f"BENCH:{run_id}", run_id]:
        assert run_id_of_key(key) is None


def test_pre_jobs() -> None:
    assert _pre_jobs("brun_x", parse_condition("queue:3")) == [
        "bench:brun_x:filler:1",
        "bench:brun_x:filler:2",
        "bench:brun_x:filler:3",
    ]
    assert _pre_jobs("brun_x", parse_condition("cache:warm")) == ["bench:brun_x:warmup"]
    for text in ["none", "cache:cold", "cpu_load:1", "unavailable:5s"]:
        assert _pre_jobs("brun_x", parse_condition(text)) == []


def test_node_pauses_expire() -> None:
    now = [100.0]
    pauses = NodePauses(lambda: now[0])
    assert not pauses.paused("node_a")
    pauses.pause("node_a", 30)
    pauses.pause("node_a", 5)  # a shorter pause never shortens a longer one
    now[0] = 129.0
    assert pauses.paused("node_a") and not pauses.paused("node_b")
    now[0] = 130.0
    assert not pauses.paused("node_a")
