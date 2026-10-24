"""Load -> evaluate -> store, for ``POST /v1/planner/eval-reports`` (V0.14 G4, D297/D316/D318).

:meth:`EvalService.run`:

1. the campaigns must exist (:class:`EvalNotFoundError` otherwise);
2. their groups, records and replays (:func:`~.loader.load_campaigns`);
3. ``holdout`` must be a family of those groups (:class:`EvalInputError`);
4. the calibrations of the calibrated rows: one leave-one-family-out refit per
   evaluated family from the matched runs (D316,
   :func:`~.heldout.refit_choices`), or with ``allow_leak`` the active version
   for all (:func:`~.heldout.leaked_choices`);
4b. (V0.15 G5a, D368/D369) the ML rows' inputs - one fold model per
   evaluated family, or ``ml_unavailable`` (:mod:`.ml_inputs`; no provider
   = ``ml_unavailable`` too);
5. the pure evaluation (:func:`~.core.evaluate`) in a worker thread - it
   replays every record under every policy and must not stall the event loop;
6. a report larger than :data:`MAX_REPORT_BYTES` is refused
   (:class:`EvalInputError`: evaluate fewer campaigns or one family);
7. the report is stored (:class:`~.store.EvalReportStore`).

Logged: ``controller.planner.eval_report`` with the campaign ids, the counts and
the verdicts (no record contents).

**Start-then-poll (V0.15 G6b, D378).** :meth:`EvalService.start` (``POST
/v1/planner/eval-reports``) checks that the campaigns exist (step 1), stores
a **running** row and runs steps 2-7 in a background task that turns the row
``done`` (the report) or ``failed`` with a ``reason``: ``not_found`` (a
campaign deleted meanwhile), ``invalid_input`` (``holdout`` names no
evaluated family, too many records, the report over the size cap),
``database``, ``crashed`` (a bug; logged with its traceback) or
``controller_restart``. One evaluation at a time per controller: a start
while one runs is :class:`EvalBusyError`. A running row this process is not
running - its controller stopped, or the final write failed - is
**answered** ``failed`` by :meth:`EvalService.status_of` (never stuck
``running``) and **stored** ``failed`` (``controller_restart``) by the
start-up sweep (:meth:`~.store.EvalReportStore.sweep_running`). The
controller's stop cancels the task (its fold child is killed) and writes
``controller_restart`` best-effort. A row read as ``running`` just before
this process stored its final state (the request raced the end of the task)
is answered ``running``: the next poll reads the stored state. Single-controller
assumption, as the session sweep's (``controller.startup_sweep``).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.calibration_store import (
    CalibrationStore,
    CalibrationStoreError,
)
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.heldout import (
    CalibrationChoice,
    leaked_choices,
    refit_choices,
)
from meshforge.controller.planner_eval.loader import (
    EvalInputs,
    EvalLoadError,
    EvalTooLargeError,
    load_campaigns,
    missing_campaigns,
)
from meshforge.controller.planner_eval.ml_inputs import EvalMlInputs
from meshforge.controller.planner_eval.model import (
    ML_UNAVAILABLE,
    EvalConfig,
    EvalMl,
    default_policies,
)
from meshforge.controller.planner_eval.report import report_text
from meshforge.controller.planner_eval.store import (
    CONTROLLER_RESTART,
    STATUS_FAILED,
    STATUS_RUNNING,
    EvalReportRow,
    EvalReportStore,
    EvalStoreError,
)

__all__ = [
    "MAX_REPORT_BYTES",
    "STOPPED_DETAIL",
    "EvalBusyError",
    "EvalInputError",
    "EvalNotFoundError",
    "EvalService",
]

log = get_logger(__name__)

MAX_REPORT_BYTES: Final = 16 * 1024 * 1024
"""Largest report stored (canonical JSON bytes)."""
STOPPED_DETAIL: Final = "the controller stopped while this evaluation ran; run it again"
_LOST_DETAIL: Final = (
    "no controller is running this evaluation (its controller stopped); run it again"
)
_FAIL_WRITE_S: Final = 5.0
"""How long the stop's best-effort ``controller_restart`` write may take."""
_MAX_UNWRITTEN: Final = 64
"""Outcomes whose final write failed, remembered for :meth:`EvalService.status_of`;
also the bound of the reports whose final state this process stored."""


class EvalNotFoundError(MeshForgeError):
    default_failure_type = FailureType.NOT_FOUND


class EvalInputError(MeshForgeError):
    default_failure_type = FailureType.INVALID_INPUT


class EvalBusyError(MeshForgeError):
    """An evaluation is already running on this controller
    (``details["report_id"]``: its report, None while it starts)."""

    default_failure_type = FailureType.CONFLICT


@dataclass(frozen=True, slots=True)
class _Running:
    report_id: int
    task: asyncio.Task[None]


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
        ml: EvalMlInputs | None = None,
    ) -> None:
        self._engine = engine
        self._calibrations = calibrations
        self.reports = reports
        self._calibration_config = calibration_config
        self._max_rows = max_rows
        self._config = config or EvalConfig()
        self._ml = ml
        self._lock = asyncio.Lock()
        self._starting = False
        self._running: _Running | None = None
        self._unwritten: OrderedDict[int, tuple[str, str | None]] = OrderedDict()
        self._written: OrderedDict[int, None] = OrderedDict()

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
            choices = await _stoppable(
                lambda cancel: refit_choices(
                    runs, families, self._calibration_config, cancel=cancel
                )
            )
        ml = (
            EvalMl(ML_UNAVAILABLE, "the controller has no ML evaluation inputs")
            if self._ml is None
            else await self._ml.prepare(families, allow_leak=allow_leak)
        )
        return await _stoppable(
            lambda cancel: evaluate(
                inputs.groups,
                inputs.records,
                choices,
                default_policies(ml=True),
                self._config,
                campaign_ids=ids,
                holdout=holdout,
                leaked=allow_leak,
                latency=inputs.latency,  # V0.14 G6 (D324)
                ml=ml,  # V0.15 G5a (D368/D369)
                cancel=cancel,
            )
        )

    async def _checked_report(
        self, campaign_ids: Sequence[str], *, holdout: str | None, allow_leak: bool
    ) -> dict[str, Any]:
        """:meth:`report` with the size cap (step 6)."""
        report = await self.report(campaign_ids, holdout=holdout, allow_leak=allow_leak)
        size = len(report_text(report).encode("utf-8"))
        if size > MAX_REPORT_BYTES:
            raise EvalInputError(
                f"the report would be {size} bytes (> {MAX_REPORT_BYTES}); "
                "evaluate fewer campaigns or one family (--holdout)",
                details={"bytes": size, "limit": MAX_REPORT_BYTES},
            )
        return report

    async def run(
        self,
        campaign_ids: Sequence[str],
        *,
        holdout: str | None = None,
        allow_leak: bool = False,
        user_id: str | None = None,
    ) -> EvalReportRow:
        """Evaluate and store, waiting for it (module docstring; the API
        uses :meth:`start`). One evaluation at a time per controller (each
        replays every record under every policy)."""
        started = time.monotonic()
        async with self._lock:
            report = await self._checked_report(
                campaign_ids, holdout=holdout, allow_leak=allow_leak
            )
            row = await self.reports.insert(report)
        _log_report(row, report, user_id=user_id, started=started)
        return row

    # ------------------------------------------------ start-then-poll (D378)

    @property
    def running_id(self) -> int | None:
        """The report this controller is evaluating now (None = none)."""
        running = self._running
        if running is None or running.task.done():
            return None
        return running.report_id

    @property
    def busy(self) -> bool:
        """An evaluation is starting or running (a new start is refused)."""
        return self._starting or self._lock.locked() or self.running_id is not None

    async def start(
        self,
        campaign_ids: Sequence[str],
        *,
        holdout: str | None = None,
        allow_leak: bool = False,
        user_id: str | None = None,
    ) -> EvalReportRow:
        """Store a running row and evaluate in the background (module
        docstring); answers the running row. Raises :class:`EvalBusyError`,
        :class:`EvalNotFoundError`, :class:`~.store.EvalStoreError`,
        :class:`~.loader.EvalLoadError`."""
        if self.busy:
            raise EvalBusyError(
                "an evaluation is already running on this controller; wait for "
                "it to end (compute planner reports) and try again",
                details={"report_id": self.running_id},
            )
        self._starting = True  # before the first await: a concurrent start is busy
        try:
            ids = sorted(set(campaign_ids))
            missing = await missing_campaigns(self._engine, ids)
            if missing:
                raise EvalNotFoundError(
                    "no such campaign: " + ", ".join(missing),
                    details={"campaign_ids": missing},
                )
            row = await self.reports.start(ids, holdout=holdout, leaked=allow_leak)
            task = asyncio.get_running_loop().create_task(
                self._execute(
                    row.id, ids, holdout=holdout, allow_leak=allow_leak, user_id=user_id
                ),
                name=f"planner-eval-{row.id}",
            )
            self._running = _Running(row.id, task)
        finally:
            self._starting = False
        log.info(
            "controller.planner.eval_started",
            report_id=row.id,
            user_id=user_id,
            campaign_ids=ids,
            holdout=holdout,
            leaked=allow_leak,
        )
        return row

    async def _execute(
        self,
        report_id: int,
        campaign_ids: list[str],
        *,
        holdout: str | None,
        allow_leak: bool,
        user_id: str | None,
    ) -> None:
        """The background evaluation of ``report_id``: never raises (but
        cancellation); the row ends ``done`` or ``failed``."""
        started = time.monotonic()
        reason: str
        detail: str | None
        try:
            async with self._lock:
                report = await self._checked_report(
                    campaign_ids, holdout=holdout, allow_leak=allow_leak
                )
                row = await self.reports.finish(report_id, report)
            self._stored(report_id)
        except asyncio.CancelledError:
            await self._fail_on_stop(report_id)
            raise
        except EvalNotFoundError as exc:
            reason, detail = "not_found", str(exc)
        except (EvalInputError, EvalTooLargeError) as exc:
            reason, detail = "invalid_input", str(exc)
        except (EvalLoadError, EvalStoreError, CalibrationStoreError) as exc:
            reason = "database"
            detail = f"the database failed ({exc.details.get('cause') or 'unknown'})"
        except Exception as exc:
            log.exception(
                "controller.planner.eval_crashed",
                report_id=report_id,
                error_type=type(exc).__name__,
            )
            reason, detail = "crashed", type(exc).__name__
        else:
            if row is not None:
                _log_report(row, report, user_id=user_id, started=started)
            else:  # no longer running in the database (swept meanwhile)
                log.warning(
                    "controller.planner.eval_result_dropped", report_id=report_id
                )
            return
        await self._fail(report_id, reason, detail, started=started)

    async def _fail(
        self, report_id: int, reason: str, detail: str | None, *, started: float
    ) -> None:
        log.warning(
            "controller.planner.eval_failed",
            report_id=report_id,
            reason=reason,
            detail=detail,
            took_s=round(time.monotonic() - started, 3),
        )
        try:
            await self.reports.fail(report_id, reason, detail)
            self._stored(report_id)
        except EvalStoreError:
            # the row stays running in the database: status_of answers this
            # outcome; the next start-up sweep stores controller_restart
            self._unwritten[report_id] = (reason, detail)
            while len(self._unwritten) > _MAX_UNWRITTEN:
                self._unwritten.popitem(last=False)
            log.warning(
                "controller.planner.eval_failure_unwritten", report_id=report_id
            )

    async def _fail_on_stop(self, report_id: int) -> None:
        """Best effort, bounded: the stopping controller marks its running
        evaluation ``controller_restart`` (else the start-up sweep does)."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.shield(
                    self.reports.fail(report_id, CONTROLLER_RESTART, STOPPED_DETAIL)
                ),
                _FAIL_WRITE_S,
            )
            self._stored(report_id)

    def _stored(self, report_id: int) -> None:
        """This process stored ``report_id``'s final state (a read that still
        says ``running`` is older than that write)."""
        self._written[report_id] = None
        while len(self._written) > _MAX_UNWRITTEN:
            self._written.popitem(last=False)

    def status_of(self, row: EvalReportRow) -> tuple[str, str | None, str | None]:
        """``(status, reason, detail)`` to answer for ``row``: a running row
        this process is not running is ``failed`` (module docstring)."""
        status = row.status
        if status != STATUS_RUNNING:
            return status, row.reason, row.detail
        if self.running_id == row.id:
            return STATUS_RUNNING, None, None
        # a start in flight does not make other running rows "running" (an
        # orphan of a dead controller stays failed - G6b tester T1); only the
        # commit-to-return instant of the row being created is not known here
        unwritten = self._unwritten.get(row.id)
        if unwritten is not None:
            return STATUS_FAILED, unwritten[0], unwritten[1]
        if row.id in self._written:
            # read before this process stored the final state: still running
            # for this answer, the next poll reads the stored state
            return STATUS_RUNNING, None, None
        return STATUS_FAILED, CONTROLLER_RESTART, _LOST_DETAIL

    async def wait(self) -> None:
        """Until the background evaluation (if any) has ended (tests)."""
        running = self._running
        if running is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(running.task)

    def request_stop(self) -> asyncio.Task[None] | None:
        """Cancel the running evaluation (the controller stops): its fold
        child is killed, its row ends ``controller_restart``. The task, for
        the caller to await (None = nothing runs)."""
        running = self._running
        if running is None or running.task.done():
            return None
        running.task.cancel()
        return running.task


async def _stoppable[T](work: Callable[[threading.Event], T]) -> T:
    """``work(cancel)`` in a worker thread; a cancelled caller (the controller
    stops) sets ``cancel`` so the thread ends at its next check instead of
    replaying on unseen (G6b reviewer N1)."""
    cancel = threading.Event()
    try:
        return await asyncio.to_thread(work, cancel)
    except asyncio.CancelledError:
        cancel.set()
        raise


def _log_report(
    row: EvalReportRow,
    report: dict[str, Any],
    *,
    user_id: str | None,
    started: float,
) -> None:
    counts = report["counts"]
    log.info(
        "controller.planner.eval_report",
        report_id=row.id,
        user_id=user_id,
        campaign_ids=row.campaign_ids,
        holdout=report["holdout"],
        leaked=bool(report["leaked"]),
        groups=counts["groups"],
        evaluated_groups=counts["evaluated_groups"],
        records_used=counts["records_used"],
        verdicts={str(v["criterion"]): v["status"] for v in report["verdicts"]},
        ml_status=report["ml"]["status"],
        ml_verdicts={str(v["name"]): v["status"] for v in report["ml"]["verdicts"]},
        latency_decisions=report["latency"]["decisions"],
        took_s=round(time.monotonic() - started, 3),
    )
