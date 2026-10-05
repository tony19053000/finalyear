"""V0.6 G3 fix round 1 (D126, D127, root cause): the pure parts.

* D126: ``disk_full`` is retried for an auto-placed job and not for a pinned
  one, whatever the agent's per-instance flag says; the node that ran out of
  disk is excluded for the window like ``node_lost``;
* D127: a cancel request outranks every way an attempt can end;
* the root-cause prefix of a failed job's detail.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.controller.jobs.retry import (
    RetryReason,
    first_failure_detail,
    retry_verdict,
)
from meshforge.controller.planner import NodeSnapshot, PastFailure, exclusions

F = FailureType
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)


def verdict(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "failure_type": F.DISK_FULL,
        "retryable": None,
        "lost": False,
        "attempts_used": 1,
        "max_attempts": 3,
        "base_backoff_s": 0.0,
        "on_lost_retry": True,
        "cancel_requested": False,
    }
    base.update(kw)
    return retry_verdict(**base)


def node(node_id: str) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-subprocess"}),
        cpu_slots=1,
    )


# ------------------------------------------------------------------ D126


@pytest.mark.parametrize("flag", [None, False, True])
def test_disk_full_of_an_auto_job_is_retried_whatever_the_agent_flag(
    flag: bool | None,
) -> None:
    got = verdict(retryable=flag, pinned=False)
    assert got.reason is RetryReason.RETRY


@pytest.mark.parametrize("flag", [None, False, True])
def test_disk_full_of_a_pinned_job_is_not_retried(flag: bool | None) -> None:
    got = verdict(retryable=flag, pinned=True)
    assert got.reason is RetryReason.NOT_RETRYABLE


def test_disk_full_still_exhausts_and_still_yields_to_a_cancel() -> None:
    assert verdict(attempts_used=3).reason is RetryReason.RETRIES_EXHAUSTED
    assert verdict(cancel_requested=True).reason is RetryReason.CANCELLED


def test_pinned_changes_nothing_for_other_types() -> None:
    assert verdict(failure_type=F.TIMEOUT, pinned=True).retry
    assert verdict(failure_type=F.USER_ERROR, pinned=False).reason is (
        RetryReason.NOT_RETRYABLE
    )
    assert verdict(failure_type=F.NODE_LOST, lost=True, pinned=True).retry


def test_the_node_that_ran_out_of_disk_is_excluded_for_the_window() -> None:
    nodes = (node("a"), node("b"))
    ended = NOW - dt.timedelta(seconds=10)
    past = [PastFailure("a", "disk_full", ended, ())]
    assert exclusions(past, nodes, now=NOW, exclude_s=600) == {"a": "disk_full"}
    # after the window the node may be tried again (GC may have freed space)
    assert exclusions(past, nodes, now=NOW, exclude_s=5) == {}


# ------------------------------------------------------------------ D127


@pytest.mark.parametrize(
    ("failure_type", "lost", "flag"),
    [
        (F.NODE_LOST, True, None),
        (F.NODE_LOST, False, True),
        (F.TIMEOUT, False, None),
        (F.TRANSFER_ERROR, False, True),
        (F.DISK_FULL, False, None),
        (F.USER_ERROR, False, None),
        (F.INTERNAL_ERROR, False, False),
    ],
)
def test_a_cancel_request_outranks_how_the_attempt_ended(
    failure_type: FailureType, lost: bool, flag: bool | None
) -> None:
    got = verdict(
        failure_type=failure_type, lost=lost, retryable=flag, cancel_requested=True
    )
    assert got.reason is RetryReason.CANCELLED
    # even on the last attempt and with on_lost off: CANCELLED, not FAILED
    got = verdict(
        failure_type=failure_type,
        lost=lost,
        retryable=flag,
        cancel_requested=True,
        attempts_used=3,
        on_lost_retry=False,
    )
    assert got.reason is RetryReason.CANCELLED


# ------------------------------------------------------------ root cause


def test_root_cause_leads_the_detail_when_the_type_changed() -> None:
    got = first_failure_detail(F.NOT_FOUND, "integrity_error", "blob gone")
    assert got == "first failure: integrity_error; blob gone"
    assert first_failure_detail(F.NOT_FOUND, "integrity_error", None) == (
        "first failure: integrity_error"
    )


def test_no_prefix_when_the_type_is_the_same_or_unknown() -> None:
    assert first_failure_detail(F.TIMEOUT, "timeout", "slow") == "slow"
    assert first_failure_detail(F.TIMEOUT, None, "slow") == "slow"
    assert first_failure_detail(F.TIMEOUT, None, None) is None
