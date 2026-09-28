"""How an agent's attempt report moves the attempt and its job (pure; V0.4 G4).

No I/O, no clock: the store (:mod:`.execution_store`) calls these inside its
transaction and the tables of :mod:`.transitions` decide every arrow.

**Attempt path (D52).** Reports arrive in order per session but can be
duplicated (replay after a reconnect) or skip states (a status dropped from the
agent's bounded ring). :func:`attempt_path` walks the §12.2 happy path from the
stored state to the reported one - ``ASSIGNED -> STAGING -> ENV_READY -> RUNNING
-> UPLOADING -> DONE`` - so a skipped report still records every arrow; a report
that is *behind* or *equal to* the stored state is a duplicate (no events); a
report for a terminal attempt that disagrees with it is :class:`StaleReportError`
(counted ``fenced``: the agent must stop, the controller already decided).

**Job events.** Reaching ``RUNNING`` starts the job, ``UPLOADING`` moves it to
``COLLECTING``, ``DONE`` succeeds it; ``FAILED`` fails it - unless the failure
type is ``cancelled``, which can only come from a ``CancelAttempt`` the
controller sent (D69), so the job becomes ``CANCELLED``. ``LOST`` fails the job
``node_lost`` (no retries until V0.6).

**Phases (D68).** :func:`phase_rows` always yields all five
:class:`~meshforge.common.states.AttemptPhase` rows. ``queue`` is the
controller's own ``QUEUED -> PREPARING`` interval from the job history; the
other four are the agent's monotonic durations. A phase with no measurement is
``skipped`` with ``duration_ms = 0`` - never a missing row, never an invented
number. ``started_at`` of the agent phases is derived: anchored at the StartAttempt
ack (else the attempt's creation) and advanced by the preceding durations.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    AttemptPhase,
    AttemptState,
    JobState,
)
from meshforge.controller.jobs.transitions import AttemptEvent, JobEvent

__all__ = [
    "AgentPhases",
    "PhaseRow",
    "StaleReportError",
    "attempt_path",
    "job_event_for",
    "phase_rows",
]

_HAPPY: Final = (
    AttemptState.ASSIGNED,
    AttemptState.STAGING,
    AttemptState.ENV_READY,
    AttemptState.RUNNING,
    AttemptState.UPLOADING,
    AttemptState.DONE,
)
_STEP: Final = {
    AttemptState.STAGING: AttemptEvent.STAGE,
    AttemptState.ENV_READY: AttemptEvent.ENV_READY,
    AttemptState.RUNNING: AttemptEvent.RUN,
    AttemptState.UPLOADING: AttemptEvent.UPLOAD,
    AttemptState.DONE: AttemptEvent.DONE,
}
AGENT_REPORTABLE: Final = frozenset({*_HAPPY, AttemptState.FAILED})
"""States an agent may report; ``LOST``/``FENCED`` are the controller's alone."""


class StaleReportError(MeshForgeError):
    """A report for an attempt the controller already ended differently."""

    default_failure_type = FailureType.FENCED


def attempt_path(
    current: AttemptState, reported: AttemptState
) -> tuple[tuple[AttemptEvent, AttemptState], ...]:
    """The arrows from ``current`` to ``reported`` (empty = duplicate/behind).

    Raises :class:`ValueError` for a state an agent may not report and
    :class:`StaleReportError` for a report that contradicts a terminal attempt.
    """
    if reported not in AGENT_REPORTABLE:
        raise ValueError(f"an agent cannot report {reported.value}")
    if current in ATTEMPT_TERMINAL_STATES:
        if reported is current:
            return ()
        raise StaleReportError(
            f"attempt is {current.value}; report says {reported.value}",
            details={"state": current.value, "reported": reported.value},
        )
    if reported is AttemptState.FAILED:
        return ((AttemptEvent.FAIL, AttemptState.FAILED),)
    here, there = _HAPPY.index(current), _HAPPY.index(reported)
    if there <= here:
        return ()
    return tuple((_STEP[state], state) for state in _HAPPY[here + 1 : there + 1])


def job_event_for(
    reached: AttemptState, failure_type: FailureType | None
) -> JobEvent | None:
    """The job event an attempt reaching ``reached`` implies (None = none)."""
    if reached is AttemptState.RUNNING:
        return JobEvent.STARTED
    if reached is AttemptState.UPLOADING:
        return JobEvent.EXITED
    if reached is AttemptState.DONE:
        return JobEvent.COLLECTED
    if reached is AttemptState.FAILED:
        return (
            JobEvent.CANCEL if failure_type is FailureType.CANCELLED else JobEvent.FAIL
        )
    if reached is AttemptState.LOST:
        return JobEvent.FAIL
    return None


# -------------------------------------------------------------------- phases

MAX_PHASE_S: Final = 8 * 24 * 3600.0
"""A phase longer than the longest job limit plus a day is not a measurement."""


@dataclass(frozen=True, slots=True)
class AgentPhases:
    """The agent's durations in seconds; ``None`` = that phase never ran."""

    stage_s: float | None = None
    env_s: float | None = None
    run_s: float | None = None
    collect_s: float | None = None

    def __post_init__(self) -> None:
        for name in ("stage_s", "env_s", "run_s", "collect_s"):
            value = getattr(self, name)
            if value is not None and not (
                math.isfinite(value) and 0 <= value <= MAX_PHASE_S
            ):
                raise ValueError(
                    f"{name} must be a finite number in [0, {MAX_PHASE_S:g}]"
                )


@dataclass(frozen=True, slots=True)
class PhaseRow:
    phase: AttemptPhase
    started_at: dt.datetime
    duration_ms: int
    skipped: bool


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def phase_rows(
    *,
    history: Sequence[tuple[JobState | None, JobState, dt.datetime]],
    anchor: dt.datetime,
    agent: AgentPhases | None,
) -> list[PhaseRow]:
    """All five phase rows of one attempt (see module docstring).

    ``history`` is the job's ``(from, to, at)`` list in order; ``anchor`` the
    StartAttempt ack time (else the attempt's creation). ``agent`` None = the
    controller ended the attempt itself (LOST, backstop): no agent phase is known.
    """
    queue_start = queue_end = None
    for from_state, to_state, at in history:
        if to_state is JobState.QUEUED:
            queue_start, queue_end = at, None
        elif from_state is JobState.QUEUED and to_state is JobState.PREPARING:
            queue_end = at
    rows: list[PhaseRow] = []
    if queue_start is not None and queue_end is not None:
        rows.append(
            PhaseRow(
                AttemptPhase.QUEUE,
                queue_start,
                _ms((queue_end - queue_start).total_seconds()),
                skipped=False,
            )
        )
    else:
        rows.append(PhaseRow(AttemptPhase.QUEUE, anchor, 0, skipped=True))
    known = agent or AgentPhases()
    at = anchor
    for phase, seconds in (
        (AttemptPhase.STAGE, known.stage_s),
        (AttemptPhase.ENV, known.env_s),
        (AttemptPhase.RUN, known.run_s),
        (AttemptPhase.COLLECT, known.collect_s),
    ):
        if seconds is None:
            rows.append(PhaseRow(phase, at, 0, skipped=True))
            continue
        rows.append(PhaseRow(phase, at, _ms(seconds), skipped=False))
        at = at + dt.timedelta(seconds=seconds)
    return rows
