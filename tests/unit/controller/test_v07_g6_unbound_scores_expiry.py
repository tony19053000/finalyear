"""V0.7 G6 item 0 (D161 N3): the unbound-score expiry helpers (no database).

* ``expire_unbound_scores_once`` passes ``now - max_age`` as the cutoff and
  never raises (a database failure is logged; the next round retries);
* ``expire_unbound_scores_forever`` runs it every ``interval_s``;
* the settings are bounded (default 7 days / 1 hour).
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from pydantic import ValidationError

from meshforge.controller.scores import (
    expire_unbound_scores_forever,
    expire_unbound_scores_once,
)
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 10, 12, tzinfo=dt.UTC)


class FakeRegistry:
    def __init__(self, fail: bool = False, rounds_wanted: int = 0) -> None:
        self.cutoffs: list[dt.datetime] = []
        self.fail = fail
        self.rounds_wanted = rounds_wanted
        self.enough = asyncio.Event()  # set once ``rounds_wanted`` rounds ran

    async def expire_unbound_scores(self, cutoff: dt.datetime) -> int:
        self.cutoffs.append(cutoff)
        if self.rounds_wanted and len(self.cutoffs) >= self.rounds_wanted:
            self.enough.set()
        if self.fail:
            raise RuntimeError("db down")
        return 2


def test_the_cutoff_is_now_minus_the_max_age() -> None:
    registry = FakeRegistry()
    count = asyncio.run(expire_unbound_scores_once(registry, 3600.0, wall=lambda: NOW))
    assert count == 2
    assert registry.cutoffs == [NOW - dt.timedelta(hours=1)]


def test_a_database_failure_is_not_raised() -> None:
    registry = FakeRegistry(fail=True)
    assert asyncio.run(expire_unbound_scores_once(registry, 1.0, wall=lambda: NOW)) == 0
    assert len(registry.cutoffs) == 1


def test_the_loop_repeats_every_interval() -> None:
    # failures do not stop the loop
    registry = FakeRegistry(fail=True, rounds_wanted=3)

    async def scenario() -> None:
        # Wait for the rounds themselves, not for a fixed wall-clock window: a
        # 50 ms window held only 1 round when the event loop was starved for a
        # few tens of ms (CI windows-latest, run 40799402426). The deadline only
        # bounds a hung loop.
        task = asyncio.create_task(
            expire_unbound_scores_forever(
                registry, max_age_s=10.0, interval_s=0.01, wall=lambda: NOW
            )
        )
        async with asyncio.timeout(30.0):
            await registry.enough.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(registry.cutoffs) >= 3
    assert set(registry.cutoffs) == {NOW - dt.timedelta(seconds=10)}


def test_the_loop_waits_one_interval_before_the_first_round() -> None:
    registry = FakeRegistry()

    async def scenario() -> None:
        task = asyncio.create_task(
            expire_unbound_scores_forever(
                registry, max_age_s=10.0, interval_s=3600.0, wall=lambda: NOW
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert registry.cutoffs == []


def test_settings_defaults_and_bounds() -> None:
    planner = PlannerSettings()
    assert planner.unbound_scores_max_age_s == 7 * 86_400.0
    assert planner.unbound_scores_check_s == 3600.0
    PlannerSettings(unbound_scores_max_age_s=0.0)
    PlannerSettings(unbound_scores_max_age_s=365 * 86_400.0)
    with pytest.raises(ValidationError):
        PlannerSettings(unbound_scores_max_age_s=-1.0)
    with pytest.raises(ValidationError):
        PlannerSettings(unbound_scores_max_age_s=365 * 86_400.0 + 1)
    with pytest.raises(ValidationError):
        PlannerSettings(unbound_scores_check_s=0.5)
