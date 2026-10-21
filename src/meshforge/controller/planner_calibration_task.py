"""The calibration refresh task (V0.14 G2, D293/D306).

:class:`CalibrationRefresher` refits the planner calibration every
``[controller.planner] calibration_refresh_s`` (1800 s) - the first time one
interval after start, so a starting controller does no extra work - and on
demand (:meth:`CalibrationRefresher.refresh_now`, ``POST
/v1/planner/calibrate`` / ``compute planner calibrate``). Each refresh is one
:meth:`~meshforge.controller.calibration_store.CalibrationStore.refresh`
transaction: fit, D306 gate, store as ``active`` or ``rejected``.

**Failure never matters to dispatch.** A refresh that fails (database down, a
bug) is logged and counted (:attr:`CalibrationRefresher.failures`); the
active version stays what it was and the dispatcher keeps planning with it.
The task itself never ends on an error. It runs only with ``calibration =
"auto"``: with ``off`` or a pinned version a new fit could not change what the
planner uses (``compute planner calibrate`` still works and says so).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Final

from meshforge.common.logging import get_logger
from meshforge.controller.calibration_store import (
    CalibrationStore,
    CalibrationStoreError,
    RefreshResult,
    row_summary,
)
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.settings import PlannerSettings

__all__ = ["CalibrationRefresher", "calibration_config"]

log = get_logger(__name__)

_MIN_INTERVAL_S: Final = 1.0


def calibration_config(settings: PlannerSettings) -> CalibrationConfig:
    """``[controller.planner] calibration_*`` as the pure fit's config."""
    low, high = settings.calibration_clamp
    return CalibrationConfig(
        window_runs=settings.calibration_window_runs,
        shrink_k=settings.calibration_shrink_k,
        clamp=(low, high),
    )


class CalibrationRefresher:
    """See module docstring. One per controller process."""

    def __init__(self, store: CalibrationStore, settings: PlannerSettings) -> None:
        self._store = store
        self._settings = settings
        self._config = calibration_config(settings)
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.refreshes = 0
        self.failures = 0
        self.last: RefreshResult | None = None

    async def refresh_now(self) -> RefreshResult:
        """One refresh (module docstring); raises
        :class:`~meshforge.controller.calibration_store.CalibrationStoreError`
        when the database fails - the caller decides (the API answers 503,
        the loop logs). Refreshes in this process run one at a time; another
        process is serialised by the store's advisory lock."""
        async with self._lock:
            result = await self._store.refresh(self._config)
        self.refreshes += 1
        self.last = result
        if result.row is None:
            log.info(
                "controller.planner.calibration_skipped",
                reason=result.skipped,
                matched_runs=result.matched_runs,
            )
        else:
            log.info(
                "controller.planner.calibration_refreshed",
                matched_runs=result.matched_runs,
                superseded_version=result.superseded,
                **row_summary(result.row),
            )
        return result

    def request_stop(self) -> None:
        """End :meth:`run` at its next wait (a refresh in progress finishes:
        it is one transaction)."""
        self._stop.set()

    async def run(self) -> None:
        """Every ``calibration_refresh_s`` until :meth:`request_stop` (or
        cancelled)."""
        interval = max(_MIN_INTERVAL_S, self._settings.calibration_refresh_s)
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)
            if self._stop.is_set():
                return
            try:
                await self.refresh_now()
            except CalibrationStoreError as exc:
                self.failures += 1
                log.warning(
                    "controller.planner.calibration_refresh_failed",
                    cause=exc.details.get("cause"),
                    failures=self.failures,
                )
            except Exception as exc:
                self.failures += 1
                log.exception(
                    "controller.planner.calibration_refresh_crashed",
                    error_type=type(exc).__name__,
                    failures=self.failures,
                )
