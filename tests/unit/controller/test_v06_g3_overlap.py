"""V0.6 G3 (D117): when a superseded attempt shows up again, the overlap is
counted once - and only when it really is one (stored LOST, the job has a
newer attempt, same fencing token). The end-to-end case with a real
partition is tests/cluster/test_v06_g3_partition.py."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import OrderedDict
from typing import Any

import pytest
import structlog

from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs.dispatch import DispatcherStats
from meshforge.controller.jobs.recovery import RecoverySteps
from meshforge.controller.jobs.store import AttemptRecord, JobRecord

NOW = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)


def _attempt(state: AttemptState, token: int = 1) -> AttemptRecord:
    return AttemptRecord(
        id="att_1",
        job_id="job_1",
        node_id="node_x",
        fencing_token=token,
        state=state,
        failure_type=None,
        detail=None,
        exit_code=None,
        output_digest=None,
        start_snapshot=None,
        started_at=None,
        ended_at=None,
        created_at=NOW,
    )


def _job(epoch: int) -> JobRecord:
    return JobRecord(
        id="job_1",
        name="j",
        state=JobState.RUNNING,
        manifest={},
        bundle_digest=None,
        placement={},
        idempotency_key=None,
        request_hash="h",
        attempt_epoch=epoch,
        failure_type=None,
        failure_detail=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeStore:
    def __init__(self, attempt: AttemptRecord | None, job: JobRecord | None) -> None:
        self.attempt, self.job = attempt, job

    async def get_attempt(self, _attempt_id: str) -> AttemptRecord | None:
        return self.attempt

    async def get_job(self, _job_id: str) -> JobRecord | None:
        return self.job


class Steps(RecoverySteps):
    def __init__(self, store: FakeStore) -> None:
        self._store = store  # type: ignore[assignment]
        self.stats = DispatcherStats()
        self._overlaps_seen = OrderedDict()


def _check(steps: Steps, token: int = 1) -> list[dict[str, Any]]:
    with structlog.testing.capture_logs() as logs:
        asyncio.run(steps._check_overlap("node_x", "att_1", token, "report"))
    return [e for e in logs if e["event"] == "controller.jobs.execution_overlap"]


def test_a_lost_attempt_of_a_retried_job_is_one_overlap_counted_once() -> None:
    steps = Steps(FakeStore(_attempt(AttemptState.LOST), _job(epoch=2)))
    (event,) = _check(steps)
    assert event["job_id"] == "job_1" and event["attempt_id"] == "att_1"
    assert event["current_attempt"] == 2 and event["source"] == "report"
    assert steps.stats.execution_overlaps == 1
    assert _check(steps) == []  # a later Reconcile / report: not again
    assert steps.stats.execution_overlaps == 1


@pytest.mark.parametrize(
    ("attempt", "job", "token"),
    [
        (None, _job(2), 1),  # unknown attempt
        (_attempt(AttemptState.LOST), _job(2), 7),  # another token
        (_attempt(AttemptState.FAILED), _job(2), 1),  # it ended by itself
        (_attempt(AttemptState.DONE), _job(1), 1),
        (_attempt(AttemptState.LOST), _job(1), 1),  # lost, never retried
        (_attempt(AttemptState.LOST), None, 1),
    ],
    ids=["unknown", "token", "failed", "done", "not_retried", "no_job"],
)
def test_no_overlap_is_counted_otherwise(
    attempt: AttemptRecord | None, job: JobRecord | None, token: int
) -> None:
    steps = Steps(FakeStore(attempt, job))
    assert _check(steps, token) == []
    assert steps.stats.execution_overlaps == 0
