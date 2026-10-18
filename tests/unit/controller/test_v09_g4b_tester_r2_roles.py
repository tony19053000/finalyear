"""V0.9 G4 lane B tester round 2: filler / warm-up tagging cannot be spoofed by
lookalike keys, and tagged rows are never ``condition_failed``; B1 regressions."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.ids import IdKind, new_id
from meshforge.controller.bench.conditions import (
    filler_key,
    pre_job_role,
    warmup_key,
)
from meshforge.controller.telemetry.conditions import condition_outcome
from meshforge.controller.telemetry.record import RecordSource, build_record

pytestmark = pytest.mark.unit

RUN = str(new_id(IdKind.BENCH_RUN))
OTHER_KIND = str(new_id(IdKind.JOB))
T0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    "key",
    [
        f"bench:{RUN}:filler:",  # empty index
        f"bench:{RUN}:filler:-1",
        f"bench:{RUN}:filler: 1",
        f"bench:{RUN}:filler:1 ",
        f"bench:{RUN}:filler:+1",
        f"bench:{RUN}:filler:1.0",
        f"bench:{RUN}:filler:0x1",
        f"bench:{RUN}:filler:²",  # superscript two
        f"bench:{RUN}:Filler:1",
        f"bench:{RUN}:WARMUP",
        f"bench:{RUN}:warmup:",
        f"bench:{RUN}::warmup",
        f"bench::{RUN}:warmup",
        f" bench:{RUN}:warmup",
        f"bench:{RUN}:warmup\n",
        f"bench:{RUN}:warmup\x00",
        f"bench:{RUN} :warmup",
        f"bench:{OTHER_KIND}:warmup",  # a job id is not a bench run id
        f"bench:{OTHER_KIND}:filler:1",
        f"xbench:{RUN}:warmup",
        f"bench:{RUN}:filler:1:warmup",
        "bench:",
        "bench::",
        "bench:::",
    ],
)
def test_lookalike_keys_are_never_tagged(key: str) -> None:
    assert pre_job_role(key) is None


def test_trailing_newline_is_not_accepted_as_the_exact_key() -> None:
    assert pre_job_role(f"bench:{RUN}:filler:1\n") is None


def test_huge_filler_index_is_still_a_filler() -> None:
    assert pre_job_role(filler_key(RUN, 10**30)) == ("filler", RUN)


def test_returned_run_id_is_the_exact_run_id() -> None:
    assert pre_job_role(warmup_key(RUN)) == ("warmup", RUN)


def test_non_ascii_decimal_digit_index_is_not_a_filler() -> None:
    assert pre_job_role(f"bench:{RUN}:filler:{chr(0x661)}") is None


@pytest.mark.parametrize("state", ["done", "failed", "cancelled", "running", "lost"])
@pytest.mark.parametrize("role", ["filler", "warmup"])
@pytest.mark.parametrize(
    "workload",
    [
        None,
        {"condition": {"spec": "netem:delay=50ms", "verified": False}},
        {"condition": {"verified": False, "reason": "x"}, "status": "failed"},
        {"condition": "garbage"},
    ],
)
def test_tagged_rows_are_never_condition_failed(
    state: str, role: str, workload: Any
) -> None:
    out = condition_outcome(
        campaign={"bench_role": role, "run_id": RUN}, workload=workload, state=state
    )
    assert out.failed is False
    assert out.condition is None


@pytest.mark.parametrize(
    "identity",
    [
        {"bench_role": "filler", "condition": 5},
        {"bench_role": "filler", "condition": None},
    ],
)
def test_role_wins_when_condition_is_not_a_string(identity: dict[str, Any]) -> None:
    out = condition_outcome(campaign=identity, workload=None, state="done")
    assert out.measured is not None and out.measured["bench_role"] == "filler"
    assert out.failed is False


@pytest.mark.parametrize("campaign", [None, [], "x", 5, {"bench_role": {}}])
def test_hostile_campaign_values_are_not_tagged_and_do_not_raise(
    campaign: Any,
) -> None:
    out = condition_outcome(campaign=campaign, workload=None, state="done")
    assert out.measured is None and out.failed is False


def test_record_for_a_tagged_failed_warmup_job_is_not_condition_failed() -> None:
    record = build_record(
        RecordSource(
            attempt_id="att_1",
            job_id="job_1",
            attempt_no=1,
            node_id="node_b",
            state="failed",
            created_at=T0,
            ended_at=T0 + dt.timedelta(seconds=3),
            exit_code=1,
            placement={"node_id": "node_b"},
            submitter_node_id="node_a",
            campaign={"bench_role": "warmup", "run_id": RUN},
            workload=None,
        )
    )
    assert record["failure_class"] != "condition_failed"
    assert record["condition_measured"]["bench_role"] == "warmup"


# ---- B1 regressions (bool facts are not numbers)


def _facts(condition: str, facts: dict[str, Any]) -> Any:
    return condition_outcome(
        campaign={"campaign_id": "c", "condition": condition, "condition_facts": facts},
        workload=None,
        state="done",
    )


@pytest.mark.parametrize("bad", [True, False, "5", None, [5], {"n": 5}])
def test_queue_fillers_ahead_non_counts_fail_without_raising(bad: Any) -> None:
    assert _facts("queue:3", {"fillers_ahead": bad}).failed is True


@pytest.mark.parametrize("bad", [True, False, "7", [1], {"a": 1}])
def test_unavailable_dispatch_time_non_numbers_never_raise(bad: Any) -> None:
    out = _facts("unavailable:30", {"dispatched_after_s": bad})
    assert out.failed is True


def test_queue_exactly_enough_fillers_is_verified() -> None:
    assert _facts("queue:3", {"fillers_ahead": 3}).failed is False
    assert _facts("queue:3", {"fillers_ahead": 2}).failed is True
