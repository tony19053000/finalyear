"""§12.1 / §12.2 as tables: every arrow allowed, every non-arrow refused (D49).

The expected arrows are written out here independently of the module, from the
ARCHITECTURE diagrams, so a table edit that is not also a §12 edit fails here.
"""

from __future__ import annotations

import itertools

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    JOB_TERMINAL_STATES,
    AttemptState,
    JobState,
)
from meshforge.controller.jobs.transitions import (
    AttemptEvent,
    IllegalTransitionError,
    JobEvent,
    next_attempt_state,
    next_job_state,
)

J, E = JobState, JobEvent
JOB_ARROWS = {
    (J.SUBMITTED, E.PLAN): J.PLANNING,
    (J.PLANNING, E.PLANNED): J.QUEUED,
    (J.PLANNING, E.NO_FEASIBLE_PLAN): J.FAILED,
    (J.QUEUED, E.DISPATCHED): J.PREPARING,
    (J.PREPARING, E.STARTED): J.RUNNING,
    (J.RUNNING, E.EXITED): J.COLLECTING,
    (J.COLLECTING, E.COLLECTED): J.SUCCEEDED,
    (J.PREPARING, E.RETRY): J.RETRYING,
    (J.RUNNING, E.RETRY): J.RETRYING,
    (J.RETRYING, E.PLAN): J.PLANNING,
    (J.PREPARING, E.FAIL): J.FAILED,
    (J.RUNNING, E.FAIL): J.FAILED,
    (J.SUBMITTED, E.CANCEL): J.CANCELLED,
    (J.QUEUED, E.CANCEL): J.CANCELLED,
    (J.PREPARING, E.CANCEL): J.CANCELLED,
    (J.RUNNING, E.CANCEL): J.CANCELLED,
    # §12.1 amendment 2026-09-28 (V0.4 D55)
    (J.COLLECTING, E.FAIL): J.FAILED,
    (J.PLANNING, E.CANCEL): J.CANCELLED,
    (J.RETRYING, E.CANCEL): J.CANCELLED,
    # §12.1 amendment 2026-09-28 (V0.4 G4, D69: a cancel while outputs are collected)
    (J.COLLECTING, E.CANCEL): J.CANCELLED,
}

A, AE = AttemptState, AttemptEvent
_LIVE = (A.ASSIGNED, A.STAGING, A.ENV_READY, A.RUNNING, A.UPLOADING)
ATTEMPT_ARROWS = {
    (A.ASSIGNED, AE.STAGE): A.STAGING,
    (A.STAGING, AE.ENV_READY): A.ENV_READY,
    (A.ENV_READY, AE.RUN): A.RUNNING,
    (A.RUNNING, AE.UPLOAD): A.UPLOADING,
    (A.UPLOADING, AE.DONE): A.DONE,
    **{(s, AE.FAIL): A.FAILED for s in _LIVE},
    **{(s, AE.LOSE): A.LOST for s in _LIVE},
    **{(s, AE.FENCE): A.FENCED for s in _LIVE},
}


@pytest.mark.parametrize(
    ("state", "event"), list(itertools.product(JobState, JobEvent)), ids=str
)
def test_every_job_pair(state: JobState, event: JobEvent) -> None:
    expected = JOB_ARROWS.get((state, event))
    if expected is None:
        with pytest.raises(IllegalTransitionError) as info:
            next_job_state(state, event)
        assert info.value.failure_type is FailureType.CONFLICT
    else:
        assert next_job_state(state, event) is expected


@pytest.mark.parametrize(
    ("state", "event"), list(itertools.product(AttemptState, AttemptEvent)), ids=str
)
def test_every_attempt_pair(state: AttemptState, event: AttemptEvent) -> None:
    expected = ATTEMPT_ARROWS.get((state, event))
    if expected is None:
        with pytest.raises(IllegalTransitionError):
            next_attempt_state(state, event)
    else:
        assert next_attempt_state(state, event) is expected


def test_terminal_states_have_no_way_out() -> None:
    for state, event in itertools.product(JOB_TERMINAL_STATES, JobEvent):
        with pytest.raises(IllegalTransitionError):
            next_job_state(state, event)
    for state, event in itertools.product(ATTEMPT_TERMINAL_STATES, AttemptEvent):
        with pytest.raises(IllegalTransitionError):
            next_attempt_state(state, event)


def test_the_v04_submission_path_exists() -> None:
    assert next_job_state(next_job_state(J.SUBMITTED, E.PLAN), E.PLANNED) is J.QUEUED
