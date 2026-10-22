"""Load -> evaluate -> store, for ``POST /v1/planner/eval-reports`` (V0.14 G4, D297/D316/D318).

:meth:`EvalService.run`:

1. the campaigns must exist (:class:`EvalNotFoundError` otherwise);
2. their groups, records and replays (:func:`~.loader.load_campaigns`);
3. ``holdout`` must be a family of those groups (:class:`EvalInputError`);
4. the calibrations of the calibrated rows: one leave-one-family-out refit per
   evaluated family from the matched runs (D316,
   :func:`~.heldout.refit_choices`), or with ``allow_leak`` the active version
   for all (:func:`~.heldout.leaked_choices`);
5. the pure evaluation (:func:`~.core.evaluate`) in a worker thread - it
   replays every record under every policy and must not stall the event loop;
6. a report larger than :data:`MAX_REPORT_BYTES` is refused
   (:class:`EvalInputError`: evaluate fewer campaigns or one family);
7. the report is stored (:class:`~.store.EvalReportStore`).

Logged: ``controller.planner.eval_report`` with the campaign ids, the counts and
the verdicts (no record contents).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.heldout import (
    CalibrationChoice,
    leaked_choices,
    refit_choices,
)
from meshforge.controller.planner_eval.loader import (
    EvalInputs,
    load_campaigns,
    missing_campaigns,
)
from meshforge.controller.planner_eval.model import EvalConfig, default_policies
from meshforge.controller.planner_eval.report import report_text
from meshforge.controller.planner_eval.store import EvalReportRow, EvalReportStore

__all__ = [
    "MAX_REPORT_BYTES",
    "EvalInputError",
    "EvalNotFoundError",
    "EvalService",
]

log = get_logger(__name__)

MAX_REPORT_BYTES: Final = 16 * 1024 * 1024
"""Largest report stored (canonical JSON bytes)."""


class EvalNotFoundError(MeshForgeError):
    default_failure_type = FailureType.NOT_FOUND


class EvalInputError(MeshForgeError):
    default_failure_type = FailureType.INVALID_INPUT


def _families(inputs: EvalInputs, holdout: str | None) -> dict[str, frozenset[str]]:
    """Evaluated family -> the fingerprints of its records (the held-out
    filter's second clause, :mod:`.heldout`)."""
    family_of = {(g.campaign_id, g.group_id): g.family for g in inputs.groups}
    out: dict[str, set[str]] = {
        g.family: set() for g in inputs.groups if holdout is None or g.family == holdout
    }
    for record in inputs.records:
        family = family_of.get((record.campaign_id, record.group_id))
        if family in out and record.fingerprint is not None:
            out[family].add(record.fingerprint)
    return {family: frozenset(prints) for family, prints in out.items()}


class EvalService:
    """See module docstring."""

    def __init__(
        self,
        engine: AsyncEngine,
        calibrations: CalibrationStore,
        reports: EvalReportStore,
        calibration_config: CalibrationConfig,
        *,
        max_rows: int,
        config: EvalConfig | None = None,
    ) -> None:
        self._engine = engine
        self._calibrations = calibrations
        self.reports = reports
        self._calibration_config = calibration_config
        self._max_rows = max_rows
        self._config = config or EvalConfig()
        self._lock = asyncio.Lock()

    async def report(
        self,
        campaign_ids: Sequence[str],
        *,
        holdout: str | None = None,
        allow_leak: bool = False,
    ) -> dict[str, Any]:
        """The report (not stored). Raises :class:`EvalNotFoundError`,
        :class:`EvalInputError`, :class:`~.loader.EvalTooLargeError`,
        :class:`~.loader.EvalLoadError`,
        :class:`~meshforge.controller.calibration_store.CalibrationStoreError`."""
        ids = sorted(set(campaign_ids))
        missing = await missing_campaigns(self._engine, ids)
        if missing:
            raise EvalNotFoundError(
                "no such campaign: " + ", ".join(missing),
                details={"campaign_ids": missing},
            )
        inputs = await load_campaigns(self._engine, ids, max_rows=self._max_rows)
        families = _families(inputs, holdout)
        if holdout is not None and holdout not in families:
            raise EvalInputError(
                f"no group of family {holdout!r} in these campaigns",
                details={"holdout": holdout},
            )
        choices: dict[str, CalibrationChoice]
        if allow_leak:
            active = await self._calibrations.active_row()
            choices = leaked_choices(
                families, None if active is None else active.calibration
            )
        else:
            runs = await self._calibrations.matched_runs(self._calibration_config)
            choices = await asyncio.to_thread(
                refit_choices, runs, families, self._calibration_config
            )
        return await asyncio.to_thread(
            evaluate,
            inputs.groups,
            inputs.records,
            choices,
            default_policies(),
            self._config,
            campaign_ids=ids,
            holdout=holdout,
            leaked=allow_leak,
        )

    async def run(
        self,
        campaign_ids: Sequence[str],
        *,
        holdout: str | None = None,
        allow_leak: bool = False,
        user_id: str | None = None,
    ) -> EvalReportRow:
        """Evaluate and store (module docstring). One evaluation at a time
        per controller (each replays every record under every policy)."""
        started = time.monotonic()
        async with self._lock:
            report = await self.report(
                campaign_ids, holdout=holdout, allow_leak=allow_leak
            )
            size = len(report_text(report).encode("utf-8"))
            if size > MAX_REPORT_BYTES:
                raise EvalInputError(
                    f"the report would be {size} bytes (> {MAX_REPORT_BYTES}); "
                    "evaluate fewer campaigns or one family (--holdout)",
                    details={"bytes": size, "limit": MAX_REPORT_BYTES},
                )
            row = await self.reports.insert(report)
        counts = report["counts"]
        log.info(
            "controller.planner.eval_report",
            report_id=row.id,
            user_id=user_id,
            campaign_ids=row.campaign_ids,
            holdout=holdout,
            leaked=allow_leak,
            groups=counts["groups"],
            evaluated_groups=counts["evaluated_groups"],
            records_used=counts["records_used"],
            verdicts={str(v["criterion"]): v["status"] for v in report["verdicts"]},
            took_s=round(time.monotonic() - started, 3),
        )
        return row
