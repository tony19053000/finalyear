"""V0.9 G3 lane B tester r1: shadow plan (D215) - what the planner would have
chosen must not depend on which slots happen to be busy at the instant a run is
dispatched, or the dataset's ``planner_node_id`` collapses onto the forced node
(the lane's own cluster test saw 12/12 agreement)."""

from __future__ import annotations

import copy
import dataclasses

import pytest
from test_v09_g3_shadow_plan import A, B, _fabric, _job, _planner

from meshforge.controller.jobs.execution_store import Dispatch

pytestmark = pytest.mark.unit


def test_shadow_choice_is_stable_when_the_preferred_node_is_busy() -> None:
    idle = _fabric()
    busy = _fabric()
    busy.used[A] = [2, 0]  # both of A's cpu slots occupied by other live attempts
    idle_decision = _planner().decide(_job(B), idle)
    busy_decision = _planner().decide(_job(B), busy)
    assert isinstance(idle_decision, Dispatch) and idle_decision.record is not None
    assert isinstance(busy_decision, Dispatch) and busy_decision.record is not None
    assert idle_decision.record.shadow_chosen_node_id == A
    assert busy_decision.record.shadow_chosen_node_id == A, (
        "the would-have-chosen node flipped to the forced node only because the "
        "other node's slots were busy"
    )


def test_shadow_does_not_mutate_the_fabric_rows() -> None:
    fabric = _fabric()

    def rows(f: object) -> dict[str, object]:
        out = dataclasses.asdict(f)  # type: ignore[call-overload]
        out.pop("snapshot")  # the tick's cache, set by decide() itself
        return out

    before = copy.deepcopy(rows(fabric))
    _planner().decide(_job(B), fabric)
    assert rows(fabric) == before
