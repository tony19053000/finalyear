"""V0.7 G7 tester r1 (CG-E2 residual): the D117 overlap check ignores WHICH node
claims a superseded attempt. Node B, claiming A's LOST attempt in a Reconcile
(or in a forged report that the CG-E2 fix refuses), still gets an
``execution_overlap`` counted and logged against B - and the once-per-attempt
memory then hides the real overlap if A shows up later. Telemetry poisoning,
not a state change."""

from __future__ import annotations

import asyncio

import pytest
import structlog
from test_v06_g3_overlap import FakeStore, Steps, _attempt, _job

from meshforge.common.states import AttemptState

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _claim(steps: Steps, node: str) -> list[dict[str, object]]:
    with structlog.testing.capture_logs() as logs:
        asyncio.run(steps._check_overlap(node, "att_1", 1, "reconcile"))
    return [e for e in logs if e["event"] == "controller.jobs.execution_overlap"]


def test_control_the_owning_node_counts_one_overlap() -> None:
    steps = Steps(FakeStore(_attempt(AttemptState.LOST), _job(epoch=2)))
    assert len(_claim(steps, "node_x")) == 1  # the fake attempt is placed on node_x


@pytest.mark.xfail(
    strict=True,
    reason="TQ-7: _check_overlap never compares the claiming node with attempt.node_id",
)
def test_another_nodes_claim_is_not_an_overlap_and_does_not_use_up_the_real_one() -> None:
    steps = Steps(FakeStore(_attempt(AttemptState.LOST), _job(epoch=2)))
    assert _claim(steps, "node_other") == []
    assert steps.stats.execution_overlaps == 0
    assert len(_claim(steps, "node_x")) == 1  # the real owner is still detected
