"""Pure attempt/job progress rules (V0.4 G4: D52 idempotency, D68 phases)."""

from __future__ import annotations

import datetime as dt
import itertools
import math

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    AttemptPhase,
    AttemptState,
    JobState,
)
from meshforge.controller.jobs.progress import (
    AGENT_REPORTABLE,
    MAX_PHASE_S,
    AgentPhases,
    PhaseRow,
    StaleReportError,
    attempt_path,
    job_event_for,
    phase_rows,
)
from meshforge.controller.jobs.transitions import AttemptEvent, JobEvent

pytestmark = pytest.mark.unit

A = AttemptState
HAPPY = [A.ASSIGNED, A.STAGING, A.ENV_READY, A.RUNNING, A.UPLOADING, A.DONE]


@pytest.mark.parametrize(("current", "reported"), list(itertools.product(HAPPY, HAPPY)))
def test_happy_path_is_walked_forward_and_never_backward(
    current: AttemptState, reported: AttemptState
) -> None:
    if current is A.DONE and reported is not A.DONE:
        with pytest.raises(StaleReportError):
            attempt_path(current, reported)
        return
    path = attempt_path(current, reported)
    if HAPPY.index(reported) <= HAPPY.index(current):
        assert path == ()  # duplicate or reordered: no change
    else:
        assert [s for _, s in path] == HAPPY[
            HAPPY.index(current) + 1 : HAPPY.index(reported) + 1
        ]


def test_failed_is_one_arrow_from_any_live_state() -> None:
    for state in HAPPY[:-1]:
        assert attempt_path(state, A.FAILED) == ((AttemptEvent.FAIL, A.FAILED),)


@pytest.mark.parametrize("terminal", sorted(ATTEMPT_TERMINAL_STATES))
def test_a_terminal_attempt_accepts_only_its_own_state_again(
    terminal: AttemptState,
) -> None:
    for reported in AGENT_REPORTABLE:
        if reported is terminal:
            assert attempt_path(terminal, reported) == ()
        else:
            with pytest.raises(StaleReportError):
                attempt_path(terminal, reported)


@pytest.mark.parametrize("state", [A.LOST, A.FENCED])
def test_an_agent_cannot_report_controller_states(state: AttemptState) -> None:
    with pytest.raises(ValueError, match="cannot report"):
        attempt_path(A.RUNNING, state)


def test_job_events() -> None:
    assert job_event_for(A.RUNNING, None) is JobEvent.STARTED
    assert job_event_for(A.UPLOADING, None) is JobEvent.EXITED
    assert job_event_for(A.DONE, None) is JobEvent.COLLECTED
    assert job_event_for(A.FAILED, FailureType.USER_ERROR) is JobEvent.FAIL
    assert job_event_for(A.FAILED, FailureType.CANCELLED) is JobEvent.CANCEL
    assert job_event_for(A.LOST, FailureType.NODE_LOST) is JobEvent.FAIL
    assert job_event_for(A.STAGING, None) is None


T0 = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)
HISTORY = [
    (None, JobState.SUBMITTED, T0),
    (JobState.SUBMITTED, JobState.PLANNING, T0),
    (JobState.PLANNING, JobState.QUEUED, T0 + dt.timedelta(milliseconds=5)),
    (JobState.QUEUED, JobState.PREPARING, T0 + dt.timedelta(seconds=2)),
]


def by_phase(rows: list[PhaseRow]) -> dict[AttemptPhase, PhaseRow]:
    return {r.phase: r for r in rows}


def test_all_five_phases_measured() -> None:
    rows = phase_rows(
        history=HISTORY,
        anchor=T0 + dt.timedelta(seconds=2),
        agent=AgentPhases(
            stage_s=0.5, env_s=1.0, run_s=3.0, collect_s=0.25, compute_ns=2_500_400_000
        ),
    )
    assert [r.phase for r in rows] == list(AttemptPhase)
    # V0.9 G1: compute lies inside run (same start), ms rounded.
    assert by_phase(rows)[AttemptPhase.COMPUTE].duration_ms == 2500
    assert (
        by_phase(rows)[AttemptPhase.COMPUTE].started_at
        == by_phase(rows)[AttemptPhase.RUN].started_at
    )
    by = {r.phase: r for r in rows}
    assert by[AttemptPhase.QUEUE].duration_ms == 1995
    assert by[AttemptPhase.RUN].duration_ms == 3000
    assert by[AttemptPhase.RUN].started_at == T0 + dt.timedelta(seconds=3.5)
    assert not any(r.skipped for r in rows)


def test_a_phase_that_never_ran_is_skipped_not_missing_not_invented() -> None:
    rows = phase_rows(
        history=HISTORY,
        anchor=T0,
        agent=AgentPhases(stage_s=0.5, env_s=None, run_s=None, collect_s=None),
    )
    assert len(rows) == 6  # V0.9 G1: + compute
    by = {r.phase: r for r in rows}
    for phase in (
        AttemptPhase.ENV,
        AttemptPhase.RUN,
        AttemptPhase.COLLECT,
        AttemptPhase.COMPUTE,
    ):
        assert by[phase].skipped and by[phase].duration_ms == 0


def test_controller_ended_attempt_has_only_the_queue_phase_measured() -> None:
    rows = phase_rows(history=HISTORY, anchor=T0, agent=None)
    assert [r.skipped for r in rows] == [False, True, True, True, True, True]


def test_no_queue_interval_in_history_is_skipped() -> None:
    rows = phase_rows(history=HISTORY[:3], anchor=T0, agent=None)
    assert rows[0].phase is AttemptPhase.QUEUE and rows[0].skipped


@pytest.mark.parametrize(
    "bad", [-0.001, math.nan, math.inf, MAX_PHASE_S + 1.0], ids=str
)
def test_phase_bounds(bad: float) -> None:
    AgentPhases(run_s=MAX_PHASE_S)  # the maximum is accepted
    with pytest.raises(ValueError, match="run_s"):
        AgentPhases(run_s=bad)
