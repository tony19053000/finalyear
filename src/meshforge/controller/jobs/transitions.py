"""The job and attempt state machines as data (ARCHITECTURE §12.1 / §12.2, D49).

Pure: no I/O, no clock. Every state change in the controller goes through
:func:`next_job_state` / :func:`next_attempt_state` (the only writer,
:class:`~meshforge.controller.jobs.store.JobStore`, calls them before its
compare-and-set UPDATE), so an arrow that is not in these tables cannot happen -
it raises :class:`IllegalTransitionError` (``conflict``).

The tables are exactly the diagrams of §12. Arrows the diagrams do not have are
refused on purpose, even where a later version will need one: adding an arrow is an
ARCHITECTURE §12 change first, then one line here and a test. The 2026-09-28
amendments (V0.4 D55: ``COLLECTING -> FAILED``, ``PLANNING -> CANCELLED``,
``RETRYING -> CANCELLED``; G4/D69: ``COLLECTING -> CANCELLED``) are in both.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.states import AttemptState, JobState

__all__ = [
    "ATTEMPT_TRANSITIONS",
    "JOB_TRANSITIONS",
    "AttemptEvent",
    "IllegalTransitionError",
    "JobEvent",
    "next_attempt_state",
    "next_job_state",
]


class IllegalTransitionError(MeshForgeError):
    """The requested event has no arrow from the current state."""

    default_failure_type = FailureType.CONFLICT


class JobEvent(StrEnum):
    PLAN = "plan"
    """SUBMITTED/RETRYING -> PLANNING."""
    PLANNED = "planned"
    """PLANNING -> QUEUED (a feasible plan was chosen)."""
    NO_FEASIBLE_PLAN = "no_feasible_plan"
    """PLANNING -> FAILED."""
    DISPATCHED = "dispatched"
    """QUEUED -> PREPARING (an attempt was sent)."""
    STARTED = "started"
    """PREPARING -> RUNNING."""
    EXITED = "exited"
    """RUNNING -> COLLECTING (workload finished, outputs being collected)."""
    COLLECTED = "collected"
    """COLLECTING -> SUCCEEDED."""
    RETRY = "retry"
    """PREPARING/RUNNING -> RETRYING (retryable attempt failure)."""
    FAIL = "fail"
    """PREPARING/RUNNING/COLLECTING -> FAILED (non-retryable, retries exhausted,
    or the output upload failed)."""
    CANCEL = "cancel"
    """SUBMITTED/PLANNING/QUEUED/PREPARING/RUNNING/COLLECTING/RETRYING -> CANCELLED."""


class AttemptEvent(StrEnum):
    STAGE = "stage"
    ENV_READY = "env_ready"
    RUN = "run"
    UPLOAD = "upload"
    DONE = "done"
    FAIL = "fail"
    LOSE = "lose"
    FENCE = "fence"


def _freeze[S, E](
    table: dict[tuple[S, E], S],
) -> Mapping[tuple[S, E], S]:
    return MappingProxyType(table)


J, JE = JobState, JobEvent
JOB_TRANSITIONS: Final[Mapping[tuple[JobState, JobEvent], JobState]] = _freeze(
    {
        (J.SUBMITTED, JE.PLAN): J.PLANNING,
        (J.RETRYING, JE.PLAN): J.PLANNING,
        (J.PLANNING, JE.PLANNED): J.QUEUED,
        (J.PLANNING, JE.NO_FEASIBLE_PLAN): J.FAILED,
        (J.QUEUED, JE.DISPATCHED): J.PREPARING,
        (J.PREPARING, JE.STARTED): J.RUNNING,
        (J.RUNNING, JE.EXITED): J.COLLECTING,
        (J.COLLECTING, JE.COLLECTED): J.SUCCEEDED,
        (J.PREPARING, JE.RETRY): J.RETRYING,
        (J.RUNNING, JE.RETRY): J.RETRYING,
        (J.PREPARING, JE.FAIL): J.FAILED,
        (J.RUNNING, JE.FAIL): J.FAILED,
        (J.COLLECTING, JE.FAIL): J.FAILED,  # D55
        (J.SUBMITTED, JE.CANCEL): J.CANCELLED,
        (J.PLANNING, JE.CANCEL): J.CANCELLED,  # D55
        (J.QUEUED, JE.CANCEL): J.CANCELLED,
        (J.PREPARING, JE.CANCEL): J.CANCELLED,
        (J.RUNNING, JE.CANCEL): J.CANCELLED,
        (J.COLLECTING, JE.CANCEL): J.CANCELLED,  # D69 (G4)
        (J.RETRYING, JE.CANCEL): J.CANCELLED,  # D55
    }
)

A, AE = AttemptState, AttemptEvent
_LIVE_ATTEMPT: Final = (A.ASSIGNED, A.STAGING, A.ENV_READY, A.RUNNING, A.UPLOADING)
ATTEMPT_TRANSITIONS: Final[Mapping[tuple[AttemptState, AttemptEvent], AttemptState]] = (
    _freeze(
        {
            (A.ASSIGNED, AE.STAGE): A.STAGING,
            (A.STAGING, AE.ENV_READY): A.ENV_READY,
            (A.ENV_READY, AE.RUN): A.RUNNING,
            (A.RUNNING, AE.UPLOAD): A.UPLOADING,
            (A.UPLOADING, AE.DONE): A.DONE,
            **{(s, AE.FAIL): A.FAILED for s in _LIVE_ATTEMPT},
            **{(s, AE.LOSE): A.LOST for s in _LIVE_ATTEMPT},
            **{(s, AE.FENCE): A.FENCED for s in _LIVE_ATTEMPT},
        }
    )
)


def next_job_state(current: JobState, event: JobEvent) -> JobState:
    """The state after ``event``; :class:`IllegalTransitionError` if no arrow."""
    try:
        return JOB_TRANSITIONS[(current, event)]
    except KeyError:
        raise IllegalTransitionError(
            f"job cannot '{event.value}' while {current.value}",
            details={"state": current.value, "event": event.value},
        ) from None


def next_attempt_state(current: AttemptState, event: AttemptEvent) -> AttemptState:
    """The state after ``event``; :class:`IllegalTransitionError` if no arrow."""
    try:
        return ATTEMPT_TRANSITIONS[(current, event)]
    except KeyError:
        raise IllegalTransitionError(
            f"attempt cannot '{event.value}' while {current.value}",
            details={"state": current.value, "event": event.value},
        ) from None
