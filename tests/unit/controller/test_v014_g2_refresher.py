"""V0.14 G2: the calibration refresh task.

It refreshes every ``calibration_refresh_s`` (never at start), keeps running
after a database failure or a bug (counted, logged), stops at
``request_stop``, and ``refresh_now`` passes the configured window / k /
clamp to the store.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from structlog.testing import capture_logs

from meshforge.controller.calibration_store import CalibrationStoreError, RefreshResult
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_calibration_task import (
    CalibrationRefresher,
    calibration_config,
)
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit


class _Store:
    def __init__(self, failures: list[BaseException | None]) -> None:
        self.failures = failures
        self.configs: list[CalibrationConfig] = []

    async def refresh(self, config: CalibrationConfig, **_: Any) -> RefreshResult:
        self.configs.append(config)
        fail = self.failures.pop(0) if self.failures else None
        if fail is not None:
            raise fail
        return RefreshResult(matched_runs=0, row=None, skipped="too_few_runs")


def test_config_comes_from_the_settings() -> None:
    settings = PlannerSettings(
        calibration_window_runs=50,
        calibration_shrink_k=2.5,
        calibration_clamp=(0.5, 3.0),
    )
    assert calibration_config(settings) == CalibrationConfig(
        window_runs=50, shrink_k=2.5, clamp=(0.5, 3.0)
    )


def test_the_loop_survives_failures_and_stops_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store([CalibrationStoreError("db down"), RuntimeError("bug"), None])
    settings = PlannerSettings(calibration_refresh_s=10.0)
    refresher = CalibrationRefresher(store, settings)  # type: ignore[arg-type]
    waits: list[float] = []
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw: Any, wait_s: float) -> Any:
        waits.append(wait_s)
        if len(waits) >= 4:
            refresher.request_stop()
        return await real_wait_for(aw, 0.001)

    monkeypatch.setattr(
        "meshforge.controller.planner_calibration_task.asyncio.wait_for", fast_wait_for
    )
    with capture_logs() as logs:
        asyncio.run(refresher.run())
    assert waits[:3] == [10.0, 10.0, 10.0]  # first refresh only after one interval
    assert refresher.failures == 2 and refresher.refreshes == 1
    events = [e["event"] for e in logs]
    assert "controller.planner.calibration_refresh_failed" in events
    assert "controller.planner.calibration_refresh_crashed" in events
    assert "controller.planner.calibration_skipped" in events
    assert len(store.configs) == 3


def test_refresh_now_raises_store_errors_to_the_caller() -> None:
    store = _Store([CalibrationStoreError("db down")])
    refresher = CalibrationRefresher(store, PlannerSettings())  # type: ignore[arg-type]
    with pytest.raises(CalibrationStoreError):
        asyncio.run(refresher.refresh_now())
    result = asyncio.run(refresher.refresh_now())
    assert result.skipped == "too_few_runs" and refresher.last is result
