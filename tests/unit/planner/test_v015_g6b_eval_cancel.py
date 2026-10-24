"""V0.15 G6b fix round 1 (reviewer N1): a stopped evaluation ends its replay
thread. ``evaluate`` / ``refit_choices`` take a ``threading.Event`` checked
before each group / family; the service's ``_stoppable`` sets it when the
awaiting task is cancelled (the controller stops)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from mf_eval_world import fabric, group, records

from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval import service
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.heldout import refit_choices
from meshforge.controller.planner_eval.model import (
    EvalCancelledError,
    EvalConfig,
    default_policies,
)

pytestmark = pytest.mark.unit


def _inputs() -> tuple[list[object], list[object]]:
    request, snapshot = fabric({"node_a": 100, "node_b": 10, "node_c": 50})
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200})
    return [g], list(records(g, request, snapshot))


def _evaluate(cancel: threading.Event | None) -> dict[str, object]:
    groups, recs = _inputs()
    return evaluate(
        groups,  # type: ignore[arg-type]
        recs,  # type: ignore[arg-type]
        {},
        default_policies(),
        EvalConfig(bootstrap_resamples=50),
        campaign_ids=["camp_x"],
        cancel=cancel,
    )


def test_evaluate_stops_when_the_flag_is_set() -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(EvalCancelledError):
        _evaluate(cancel)


def test_evaluate_with_a_clear_flag_equals_no_flag() -> None:
    assert _evaluate(threading.Event()) == _evaluate(None)


def test_refit_choices_stops_when_the_flag_is_set() -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(EvalCancelledError):
        refit_choices([], {"fam": frozenset()}, CalibrationConfig(), cancel=cancel)
    assert refit_choices([], {}, CalibrationConfig(), cancel=cancel) == {}


@pytest.mark.asyncio
async def test_a_cancelled_caller_sets_the_flag_and_the_thread_ends() -> None:
    entered = threading.Event()
    ended = threading.Event()

    def work(cancel: threading.Event) -> int:
        entered.set()
        deadline = time.monotonic() + 30
        while not cancel.is_set():  # a replay loop checking between groups
            assert time.monotonic() < deadline, "the flag was never set"
            time.sleep(0.005)
        ended.set()
        return 1

    task = asyncio.create_task(service._stoppable(work))
    await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(ended.wait, 10)
