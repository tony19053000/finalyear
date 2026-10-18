"""V0.9 G4 lane B tester round 1: the record's condition columns under hostile input.

0.9.5 criteria: an unverified condition is never labelled (``condition_failed``);
the condition columns have ONE writer (``condition_outcome``) and the single-writer
sanitisation (NaN/inf -> null, non-canonical -> null, record kept) holds for every
source of text/number that reaches them - the agent's report, the controller facts
and the campaign's session label.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.bench.conditions import NodePauses
from meshforge.controller.telemetry.conditions import condition_outcome
from meshforge.controller.telemetry.record import RecordSource, build_record
from meshforge.controller.telemetry.schema import FailureClass

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)


def _src(
    campaign: dict[str, Any] | None, workload: dict[str, Any] | None, **kw: Any
) -> RecordSource:
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
        "campaign": campaign,
        "workload": {
            "family": "cpu.sort",
            "params": {"n": 10},
            "status": "ok",
            **(workload or {}),
        },
    }
    values.update(kw)
    return RecordSource(**values)


def _camp(condition: Any, **extra: Any) -> dict[str, Any]:
    return {"campaign_id": "camp_1", "condition": condition, **extra}


def _rep(spec: Any, verified: Any = True, **extra: Any) -> dict[str, Any]:
    return {
        "condition": {"spec": spec, "verified": verified, "reverted": True, **extra}
    }


# --------------------------------------------------------- classification


@pytest.mark.parametrize("verified", ["yes", 1, "true", None, [], {}, 2])
def test_only_a_real_true_counts_as_verified(verified: Any) -> None:
    """A truthy-but-not-True ``verified`` (hostile / buggy agent) is unverified."""
    out = condition_outcome(
        campaign=_camp("cpu_load:1"),
        workload=_rep("cpu_load:1", verified),
        state="done",
    )
    assert out.failed is True
    assert out.measured is not None and out.measured["verified"] is False


@pytest.mark.parametrize(
    "spec", [None, 5, ["cpu_load:1"], {}, "cpu_load:1 ", "CPU_LOAD:1", ""]
)
def test_a_report_for_another_or_garbled_spec_never_verifies(spec: Any) -> None:
    out = condition_outcome(
        campaign=_camp("cpu_load:1"), workload=_rep(spec), state="done"
    )
    assert out.failed is True


@pytest.mark.parametrize("report", ["x", 5, [1], None, True])
def test_a_non_object_condition_report_is_no_report(report: Any) -> None:
    out = condition_outcome(
        campaign=_camp("cpu_load:1"), workload={"condition": report}, state="done"
    )
    assert out.failed is True  # done + agent kind + no usable report
    assert out.measured is not None and out.measured["verified"] is False


@pytest.mark.parametrize(
    "state", ["failed", "lost", "cancelled", "timed_out", "rejected"]
)
def test_attempt_that_died_without_a_report_keeps_its_own_class(state: str) -> None:
    out = condition_outcome(campaign=_camp("cpu_load:1"), workload=None, state=state)
    assert out.failed is False
    assert out.measured is not None and out.measured["verified"] is False


def test_verified_report_never_masks_a_real_failure() -> None:
    record = build_record(
        _src(
            _camp("cpu_load:1"),
            _rep("cpu_load:1", True, measured={}),
            state="failed",
            failure_type="node_lost",
            exit_code=None,
        )
    )
    assert record["failure_class"] != FailureClass.OK.value
    assert record["failure_class"] != FailureClass.CONDITION_FAILED.value
    assert record["condition"] == "cpu_load:1"


def test_unverified_done_run_is_never_ok() -> None:
    record = build_record(
        _src(_camp("netem:delay=50ms"), _rep("netem:delay=50ms", False, reason="rtt"))
    )
    assert record["failure_class"] == FailureClass.CONDITION_FAILED.value
    assert record["condition_measured"]["verified"] is False
    assert record["condition_measured"]["reason"] == "rtt"


@pytest.mark.parametrize("ahead", [True, "2", 2.0, None, -1, 0, 1])
def test_queue_with_too_few_or_odd_fillers_ahead_is_unverified(ahead: Any) -> None:
    out = condition_outcome(
        campaign=_camp("queue:2", condition_facts={"fillers_ahead": ahead}),
        workload=None,
        state="done",
    )
    assert out.failed is True


_FMT_BUG = pytest.mark.xfail(
    strict=True,
    raises=(ValueError, TypeError),
    reason="DEFECT TR1-B1 (minor): condition_outcome formats a non-numeric "
    "dispatched_after_s with :g and raises; build_record then fails the record",
)


@pytest.mark.parametrize(
    "after",
    [
        pytest.param("2", marks=_FMT_BUG),
        None,
        pytest.param([], marks=_FMT_BUG),
        math.nan,
        math.inf,
    ],
)
def test_unavailable_with_odd_dispatch_time_is_unverified(after: Any) -> None:
    out = condition_outcome(
        campaign=_camp(
            "unavailable:30s", condition_facts={"dispatched_after_s": after}
        ),
        workload=None,
        state="done",
    )
    assert out.failed is True


def test_pause_boundary_is_exclusive_and_a_lost_pause_is_never_labelled() -> None:
    def run(after: float, **facts: Any) -> bool:
        return condition_outcome(
            campaign=_camp(
                "unavailable:30s",
                condition_facts={"dispatched_after_s": after, **facts},
            ),
            workload=None,
            state="done",
        ).failed

    assert run(29.999) is False
    assert run(30.0) is True  # dispatched when the pause ended: not unavailable
    assert run(0.0, pause_lost=True) is True


@pytest.mark.parametrize("warm_state", ["succeeded", "failed", None, "cancelled"])
def test_cache_warm_is_verified_by_the_agent_report_not_the_warmup_state(
    warm_state: Any,
) -> None:
    # the warm-up's state is only recorded; the agent's "nothing fetched" decides
    ok = condition_outcome(
        campaign=_camp("cache:warm", condition_facts={"warmup_state": warm_state}),
        workload=_rep("cache:warm", True, measured={"blobs_hit": 3}),
        state="done",
    )
    assert ok.failed is False
    assert ok.measured is not None and ok.measured["controller"] == {
        "warmup_state": warm_state
    }
    bad = condition_outcome(
        campaign=_camp("cache:warm", condition_facts={"warmup_state": warm_state}),
        workload=_rep("cache:warm", False, reason="not warm"),
        state="done",
    )
    assert bad.failed is True


# --------------------------------------------------------- sanitisation


def _storable(record: dict[str, Any]) -> None:
    """What PostgreSQL JSONB / text would accept."""
    cell = record["condition_measured"]
    if cell is not None:
        text = json.dumps(cell, allow_nan=False)
        assert "\\u0000" not in text
    cond = record["condition"]
    if cond is not None:
        assert "\x00" not in cond and len(cond) <= 128
        cond.encode("utf-8")


@pytest.mark.parametrize(
    "label",
    [
        "wifi",
        "x" * 10_000,
        "nul\x00label",
        "lone\ud800",
        "名" * 500,
        5,
        ["a"],
        {"a": 1},
        math.nan,
        None,
    ],
)
def test_hostile_session_label_never_loses_or_poisons_the_record(label: Any) -> None:
    record = build_record(_src(_camp("none", session_label=label), None))
    _storable(record)
    assert record["condition"] == "none"
    assert record["failure_class"] == FailureClass.OK.value
    got = record["condition_measured"]
    if got is not None:
        assert got["session_label"] is None or isinstance(got["session_label"], str)


@pytest.mark.parametrize(
    "measured",
    [
        {"a": math.nan},
        {"a": [math.inf, {"b": -math.inf}]},
        {"a\x00": 1},
        {"a": "v\x00"},
        {"a": "lone\ud800"},
        {1: "int key"},
        {
            "d": {
                "d": {
                    "d": {
                        "d": {
                            "d": {
                                "d": {"d": {"d": {"d": {"d": {"d": {"d": {"d": 1}}}}}}}
                            }
                        }
                    }
                }
            }
        },
    ],
)
def test_hostile_agent_measurements_never_lose_the_record(
    measured: dict[str, Any],
) -> None:
    record = build_record(
        _src(_camp("cpu_load:1"), _rep("cpu_load:1", True, measured=measured))
    )
    _storable(record)
    assert record["condition"] == "cpu_load:1"
    assert record["failure_class"] in (
        FailureClass.OK.value,
        FailureClass.CONDITION_FAILED.value,
    )


@pytest.mark.parametrize(
    "facts",
    [
        {"fillers_ahead": 2, "x": math.nan},
        {"fillers_ahead": 2, "x": "\x00"},
        {"fillers_ahead": 2, **{f"k{i}": i for i in range(3000)}},
        "not an object",
        5,
        [1],
        None,
    ],
)
def test_hostile_controller_facts_never_lose_the_record(facts: Any) -> None:
    record = build_record(_src(_camp("queue:2", condition_facts=facts), None))
    _storable(record)
    assert record["condition"] == "queue:2"


@pytest.mark.parametrize(
    "reason", ["x" * 100_000, "bad\x00", "lone\ud800", 5, None, ["a"]]
)
def test_hostile_agent_reason_never_loses_the_record(reason: Any) -> None:
    record = build_record(
        _src(_camp("cpu_load:1"), _rep("cpu_load:1", False, reason=reason))
    )
    _storable(record)
    assert record["failure_class"] == FailureClass.CONDITION_FAILED.value


@pytest.mark.parametrize(
    "stored",
    [
        "cpu_load:1 ",
        "CPU_LOAD:1",
        "queue:0",
        "a" * 5000,
        "\x00",
        "cpu_load:1\x00",
        "lone\ud800",
        "bogus:1",
        "netem:delay=9999ms",
    ],
)
def test_unparsable_stored_condition_is_null_failed_never_echoed(stored: str) -> None:
    record = build_record(_src(_camp(stored), _rep("cpu_load:1", True)))
    _storable(record)
    assert record["condition"] is None
    assert record["failure_class"] == FailureClass.CONDITION_FAILED.value
    assert record["condition_measured"]["verified"] is False


@pytest.mark.parametrize(
    ("stored", "canonical"),
    [
        ("cpu_load:01", "cpu_load:1"),
        ("netem:loss=1.0%", "netem:loss=1%"),
        ("netem:loss=1%,delay=50ms", "netem:delay=50ms,loss=1%"),
        ("gpu_load:50%", "gpu_load:50"),
    ],
)
def test_parsable_but_non_canonical_stored_condition_is_stored_canonical(
    stored: str, canonical: str
) -> None:
    kind = canonical.partition(":")[0]
    record = build_record(_src(_camp(stored), _rep(canonical, True)))
    assert record["condition"] == canonical, kind


def test_non_campaign_job_has_null_condition_columns_whatever_the_workload_says() -> (
    None
):
    record = build_record(_src(None, _rep("cpu_load:1", True, measured={"a": 1})))
    assert record["condition"] is None and record["condition_measured"] is None
    assert record["failure_class"] == FailureClass.OK.value


# --------------------------------------------------------- pauses


def test_pause_zero_negative_and_nan_durations_do_not_wedge_a_node() -> None:
    now = [0.0]
    pauses = NodePauses(lambda: now[0])
    pauses.pause("n", 0)
    assert not pauses.paused("n")
    pauses.pause("n", -5)
    assert not pauses.paused("n")
    pauses.pause("n", 10)
    now[0] = 10.0
    assert not pauses.paused("n")  # expires exactly at T
    assert not pauses.paused("n")  # and stays expired (entry dropped)


def test_pause_of_many_nodes_is_independent() -> None:
    now = [0.0]
    pauses = NodePauses(lambda: now[0])
    for i in range(500):
        pauses.pause(f"n{i}", i + 1)
    now[0] = 250.0
    assert [pauses.paused(f"n{i}") for i in (0, 248, 249, 250, 499)] == [
        False,
        False,
        False,
        True,
        True,
    ]
