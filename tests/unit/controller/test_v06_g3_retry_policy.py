"""V0.6 G3 (D110, ADR-0015): the pure retry verdict and backoff."""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import attempt_state_to_proto, failure_type_to_proto
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.jobs.retry import (
    MAX_BACKOFF_S,
    RetryReason,
    backoff_s,
    exhausted_detail,
    retry_verdict,
)

F = FailureType
JOB_ID = str(new_job_id())
ATTEMPT_ID = str(new_attempt_id())


def verdict(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "failure_type": F.TRANSFER_ERROR,
        "retryable": None,
        "lost": False,
        "attempts_used": 1,
        "max_attempts": 3,
        "base_backoff_s": 5.0,
        "on_lost_retry": True,
        "cancel_requested": False,
    }
    base.update(kw)
    return retry_verdict(**base)


@pytest.mark.parametrize(
    ("failure_type", "expected"),
    [
        (F.NODE_LOST, True),
        (F.TRANSFER_ERROR, True),
        (F.INTEGRITY_ERROR, True),
        (F.TIMEOUT, True),
        (F.VRAM_OOM, True),
        (F.RESOURCE_BUSY, True),
        (F.NETWORK_ERROR, True),
        (F.USER_ERROR, False),
        (F.OOM, False),
        (F.ENV_ERROR, False),
        (F.DISK_FULL, True),  # D126: auto-placed (pinned: test_v06_g3_fix1_policy)
        (F.DEADLINE_EXCEEDED, False),
        (F.OUTPUT_ERROR, False),
        (F.INTERNAL_ERROR, False),
    ],
)
def test_without_a_per_instance_flag_the_type_default_decides(
    failure_type: FailureType, expected: bool
) -> None:
    assert verdict(failure_type=failure_type).retry is expected


def test_the_per_instance_flag_wins_over_the_type_default() -> None:
    assert verdict(failure_type=F.OUTPUT_ERROR, retryable=True).retry
    got = verdict(failure_type=F.TRANSFER_ERROR, retryable=False)
    assert got.reason is RetryReason.NOT_RETRYABLE


def test_cancelled_is_never_retried_nor_anything_after_a_cancel_request() -> None:
    assert verdict(failure_type=F.CANCELLED, retryable=True).reason is (
        RetryReason.CANCELLED
    )
    assert verdict(cancel_requested=True).reason is RetryReason.CANCELLED
    assert verdict(lost=True, cancel_requested=True).reason is RetryReason.CANCELLED


def test_a_loss_retries_unless_the_job_opted_out() -> None:
    assert verdict(failure_type=F.NODE_LOST, lost=True).retry
    # an agent-reported node_lost (agent_shutdown) is a loss too
    assert verdict(failure_type=F.NODE_LOST, retryable=False).retry
    opted_out = verdict(failure_type=F.NODE_LOST, lost=True, on_lost_retry=False)
    assert opted_out.reason is RetryReason.ON_LOST_DISABLED
    reported = verdict(failure_type=F.NODE_LOST, on_lost_retry=False)
    assert reported.reason is RetryReason.ON_LOST_DISABLED
    # on_lost only concerns losses
    assert verdict(failure_type=F.TIMEOUT, on_lost_retry=False).retry


def test_the_last_allowed_attempt_exhausts_the_retries() -> None:
    assert verdict(attempts_used=2, max_attempts=3).retry
    assert verdict(attempts_used=3, max_attempts=3).reason is (
        RetryReason.RETRIES_EXHAUSTED
    )
    assert verdict(attempts_used=1, max_attempts=1).reason is (
        RetryReason.RETRIES_EXHAUSTED
    )
    # not retryable wins over exhausted: the reason stays the true one
    got = verdict(failure_type=F.USER_ERROR, attempts_used=3, max_attempts=3)
    assert got.reason is RetryReason.NOT_RETRYABLE


def test_backoff_doubles_and_is_capped() -> None:
    assert [backoff_s(n, 5.0) for n in (1, 2, 3, 4)] == [5.0, 10.0, 20.0, 40.0]
    assert backoff_s(9, 5.0) == MAX_BACKOFF_S
    assert backoff_s(10_000, 300.0) == MAX_BACKOFF_S
    assert backoff_s(1, 0.0) == 0.0
    assert backoff_s(0, 5.0) == 5.0
    assert verdict(attempts_used=2).backoff_s == 10.0


def test_exhausted_detail_names_the_count_and_keeps_the_cause() -> None:
    assert exhausted_detail(3, "boom") == "retries_exhausted after 3 attempts: boom"
    assert exhausted_detail(1, None) == "retries_exhausted after 1 attempts"


def _status(retryable: bool) -> agent_pb2.AttemptStatus:
    return agent_pb2.AttemptStatus(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        fencing_token=1,
        state=attempt_state_to_proto(AttemptState.FAILED),
        failure=common_pb2.Failure(
            failure_type=failure_type_to_proto(F.OUTPUT_ERROR),
            message="upload blip",
            retryable=retryable,
        ),
    )


def test_the_report_carries_the_agent_s_retryable_flag() -> None:
    for flag in (True, False):
        report = parse_status(_status(flag))
        assert not isinstance(report, str), report
        assert report.retryable is flag
    done = agent_pb2.AttemptStatus(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        fencing_token=1,
        state=attempt_state_to_proto(AttemptState.RUNNING),
    )
    report = parse_status(done)
    assert not isinstance(report, str) and report.retryable is None
