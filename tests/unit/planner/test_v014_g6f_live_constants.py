"""D332: the evaluator's "live node" definition stays the planner's own."""

from __future__ import annotations

import pytest

from meshforge.common.capabilities import EXEC_CAPABILITY
from meshforge.controller.planner import feasibility
from meshforge.controller.planner_eval import latency

pytestmark = pytest.mark.unit


def test_live_state_is_the_planners_online_state() -> None:
    assert latency.LIVE_STATE == feasibility._ONLINE


def test_live_capability_is_the_exec_capability() -> None:
    assert latency.LIVE_CAPABILITY == EXEC_CAPABILITY
