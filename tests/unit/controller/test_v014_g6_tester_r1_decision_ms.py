"""V0.14 G6 tester round 1 - ``decision_ms`` on every outcome path of
``DispatchPlanner.decide`` (D324), derived from the brief: every decision row
the dispatcher is going to write carries how long the decision took; an
outcome that writes no row carries nothing; the stamping never changes the
decision itself (outcome type, candidates, replay) and never produces a value
the table's check (``decision_ms >= 0``) or the evaluator would refuse.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest
from test_v014_g6_decision_ms import (
    CAPS,
    PLAIN,
    RECORD,
    SPLIT,
    SPLIT_ROW,
    _fabric,
    _job,
    _planner,
)

from meshforge.common.errors import FailureType
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchFailure,
    DispatchLanes,
    Wait,
)
from meshforge.controller.jobs.planning import with_decision_ms
from meshforge.controller.jobs.queue_reads import FabricRows

pytestmark = pytest.mark.unit

SPLIT_CAPS = [*CAPS, "exec-split"]


def _busy(fabric: FabricRows) -> FabricRows:
    """Every node's CPU slots taken (so nothing is placeable *now*, but nothing
    is statically impossible either)."""
    return dataclasses.replace(
        fabric, used={n: [row.cpu_slots or 1, 0] for n, row in fabric.nodes.items()}
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """perf_counter readings pop from this list (two per decide())."""
    readings: list[float] = []
    monkeypatch.setattr(planning_module.time, "perf_counter", lambda: readings.pop(0))
    return readings


def _stamped(outcome: Any) -> Any:
    assert outcome.record is not None, outcome
    ms = outcome.record.decision_ms
    assert ms is not None and math.isfinite(ms) and ms >= 0.0
    assert round(ms, 3) == ms
    return ms


def test_waiting_with_a_row_is_stamped_and_the_row_is_otherwise_untouched(
    clock: list[float],
) -> None:
    planner = _planner()
    fabric = _busy(_fabric(CAPS, n=2))
    clock[:] = [10.0, 10.0042]
    waited = planner.decide(_job(PLAIN), fabric)
    assert isinstance(waited, Wait) and waited.record is not None, waited
    assert _stamped(waited) == 4.2
    # the stamp is the only difference from the untimed decision
    clock[:] = [0.0, 0.0]
    again = planner._decide_isolated(_job(PLAIN), fabric)
    assert isinstance(again, Wait) and again.record is not None
    assert (
        dataclasses.replace(waited.record, decision_ms=None).candidates
        == again.record.candidates
    )
    assert waited.record.reason_key == again.record.reason_key


def test_a_pinned_job_to_an_unknown_node_is_decided_without_a_crash(
    clock: list[float],
) -> None:
    job = _job(PLAIN)
    job = dataclasses.replace(job, placement={"mode": "pinned", "node_id": "node_gone"})
    clock[:] = [1.0, 1.001]
    out = _planner().decide(job, _fabric(CAPS))
    # a pinned node that is gone is refused before any plan: no row to stamp
    assert isinstance(out, DispatchFailure) and out.record is None


def test_a_bad_stored_job_fails_without_a_row_and_without_a_time(
    clock: list[float],
) -> None:
    bad = _job({"entrypoint": []})  # no longer validates as a manifest
    clock[:] = [0.0, 1.0]
    out = _planner().decide(bad, _fabric(CAPS))
    assert isinstance(out, DispatchFailure)
    assert out.failure_type is FailureType.INTERNAL_ERROR
    assert out.record is None


def test_a_planning_bug_waits_with_nothing_to_write(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _planner()
    monkeypatch.setattr(planner, "_decide", lambda *_a: 1 / 0)
    clock[:] = [0.0, 9.0]
    assert planner.decide(_job(PLAIN), _fabric(CAPS)) == Wait()
    assert planner.planning_errors == 1
    clock[:] = [0.0, 9.0]  # and the over-budget counter still counts the time
    planner.decide(_job(PLAIN), _fabric(CAPS))
    assert planner.decisions_over_budget == 2


def test_a_split_replan_over_the_remainder_is_stamped(clock: list[float]) -> None:
    """A re-plan: the chunk queue exists and 3 of 8 items are left."""
    job = _job(SPLIT, split=SPLIT_ROW)
    fabric = dataclasses.replace(_fabric(SPLIT_CAPS, n=3), split_left={job.id: 3})
    clock[:] = [0.0, 0.0071]
    out = _planner().decide(job, fabric)
    assert isinstance(out, DispatchLanes), out
    assert out.record is not None
    assert _stamped(out) == 7.1


def test_a_split_job_that_waits_is_stamped(clock: list[float]) -> None:
    job = _job(SPLIT, split=SPLIT_ROW)
    fabric = _busy(_fabric(SPLIT_CAPS, n=2))
    clock[:] = [0.0, 0.003]
    out = _planner().decide(job, fabric)
    assert isinstance(out, Wait) and out.record is not None, out
    assert _stamped(out) == 3.0


def test_a_split_with_no_feasible_node_is_a_stamped_failure(clock: list[float]) -> None:
    clock[:] = [0.0, 0.5]
    out = _planner().decide(_job(SPLIT, split=SPLIT_ROW), _fabric(CAPS))
    assert isinstance(out, DispatchFailure) and out.record is not None
    assert _stamped(out) == 500.0


def test_a_dispatch_keeps_its_replay_and_candidates_when_stamped(
    clock: list[float],
) -> None:
    planner = _planner()
    fabric = _fabric(CAPS, n=3)
    clock[:] = [0.0, 0.1]
    stamped = planner.decide(_job(PLAIN), fabric)
    assert isinstance(stamped, Dispatch) and stamped.record is not None
    assert stamped.record.replay is not None
    assert stamped.record.candidates
    assert stamped.record.chosen_node_id in fabric.nodes


def test_a_clock_that_runs_backwards_gives_zero_not_a_negative_time(
    clock: list[float],
) -> None:
    clock[:] = [5.0, 4.0]
    out = _planner().decide(_job(PLAIN), _fabric(CAPS))
    assert _stamped(out) == 0.0


def test_a_very_slow_decision_is_stored_in_full_and_counted(
    clock: list[float],
) -> None:
    planner = _planner()
    clock[:] = [0.0, 7200.0]  # two hours
    out = planner.decide(_job(PLAIN), _fabric(CAPS))
    assert _stamped(out) == 7_200_000.0
    assert planner.decisions_over_budget == 1


@pytest.mark.parametrize("took", [math.nan, -math.inf, -1.0, -0.0])
def test_with_decision_ms_never_yields_a_negative_or_nan_time(took: float) -> None:
    stamped = with_decision_ms(Wait(RECORD), took)
    assert isinstance(stamped, Wait) and stamped.record is not None
    ms = stamped.record.decision_ms
    assert ms is not None and not math.isnan(ms) and ms >= 0.0


def test_with_decision_ms_infinity_is_left_for_the_evaluator_to_exclude() -> None:
    # perf_counter cannot give this; pinned so a change is noticed: +inf is
    # stored as-is (REAL accepts it) and the latency block excludes it
    from meshforge.controller.planner_eval.latency import LatencySample, latency_block

    stamped = with_decision_ms(Wait(RECORD), math.inf)
    assert stamped.record is not None and stamped.record.decision_ms == math.inf
    block = latency_block([LatencySample(stamped.record.decision_ms, 4)], _cfg())
    assert block["decisions"] == 0 and block["without_latency"] == 1


def _cfg() -> Any:
    from meshforge.controller.planner_eval.latency import LatencyConfig

    return LatencyConfig()


def test_stamping_twice_keeps_the_last_time_and_the_input_is_untouched() -> None:
    first = with_decision_ms(Wait(RECORD), 1.0)
    second = with_decision_ms(first, 2.0)
    assert first.record is not None and first.record.decision_ms == 1.0
    assert second.record is not None and second.record.decision_ms == 2.0
    assert RECORD.decision_ms is None
