"""V0.9 G4 lane B fix round 1 (B1): odd controller facts never crash the outcome.

A bool is an ``int`` in Python: ``fillers_ahead = True`` must not count as one
filler, and ``dispatched_after_s = True`` must not count as 1 s (nor be
formatted with ``:g``). A non-numeric dispatch time gets a generic reason.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.telemetry.conditions import condition_outcome

pytestmark = pytest.mark.unit


def _outcome(condition: str, **facts: Any) -> Any:
    return condition_outcome(
        campaign={
            "campaign_id": "camp_1",
            "condition": condition,
            "condition_facts": facts,
        },
        workload=None,
        state="done",
    )


def test_a_bool_is_not_a_filler_count() -> None:
    out = _outcome("queue:1", fillers_ahead=True)
    assert out.failed is True
    assert out.measured is not None
    assert out.measured["reason"] == "the filler jobs were not found"


def test_an_int_filler_count_still_counts() -> None:
    assert _outcome("queue:1", fillers_ahead=1).failed is False
    out = _outcome("queue:2", fillers_ahead=1)
    assert out.failed is True
    assert out.measured is not None
    assert out.measured["reason"].startswith("1 filler job(s)")


@pytest.mark.parametrize("after", [True, False, "2", [], {"s": 1}, b"1"])
def test_a_non_numeric_dispatch_time_gets_a_generic_reason(after: Any) -> None:
    out = _outcome("unavailable:30s", dispatched_after_s=after)
    assert out.failed is True
    assert out.measured is not None
    assert out.measured["reason"] == "the run's dispatch time is not valid"


@pytest.mark.parametrize("after", [math.nan, math.inf, 30, 31.5])
def test_a_numeric_late_or_odd_dispatch_time_is_formatted(after: float) -> None:
    out = _outcome("unavailable:30s", dispatched_after_s=after)
    assert out.failed is True
    assert out.measured is not None
    assert out.measured["reason"].startswith("dispatched ")


def test_a_numeric_dispatch_time_inside_the_pause_is_verified() -> None:
    assert _outcome("unavailable:30s", dispatched_after_s=2).failed is False
