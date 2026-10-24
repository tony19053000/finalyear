"""Load -> examples, for ``POST /v1/planner/ml/classifier-examples`` (V0.15
G6a, D375/D376; the 0.15.10 classifier experiment).

:meth:`ClassifierExamplesService.document`:

1. the campaigns must exist (:class:`~.service.EvalNotFoundError`);
2. their labelled groups, records and replays - the evaluator's own read
   (:func:`~.loader.load_campaigns`);
3. one leave-one-family-out calibration refit per family (D316,
   :func:`~.heldout.refit_choices`) - the classifier is compared with the
   evaluator's criterion row, planned exactly as the evaluator plans it;
4. :func:`~.classifier_examples.build_examples` in a worker thread (it
   re-plans every record once).

The document is **not** stored: ``compute planner ml classify`` trains and
evaluates the classifiers on it in the ``ml`` child on the operator's
machine (like ``compute planner ml compare`` on the dataset). One build at a
time per controller.

**Time budget** (G6a fix r1 N2, like the evaluator's ``ml_eval_timeout_s``):
one call may take ``timeout_s`` (``[controller.planner]
ml_classifier_timeout_s``) in total - waiting for another build included.
Over it, :class:`ClassifierTimeoutError` (504 ``timeout``): the lock wait and
the database reads are cancelled, the re-planning thread stops at its next
group (it is never abandoned while still running, so builds never overlap).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.classifier_examples import (
    build_examples,
    examples_document,
)
from meshforge.controller.planner_eval.heldout import refit_choices
from meshforge.controller.planner_eval.loader import (
    EvalInputs,
    load_campaigns,
    missing_campaigns,
)
from meshforge.controller.planner_eval.service import EvalNotFoundError

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ClassifierExamplesService",
    "ClassifierTimeoutError",
    "families_of",
]

log = get_logger(__name__)

DEFAULT_TIMEOUT_S: Final = 900.0
"""``[controller.planner] ml_classifier_timeout_s`` default."""


class ClassifierTimeoutError(MeshForgeError):
    """The build ran past its time budget (``details.phase`` = ``lock_wait``
    | ``load`` | ``build``)."""

    default_failure_type = FailureType.TIMEOUT
    exit_code = ExitCode.UNAVAILABLE


class _ExpiredError(Exception):
    """Raised by the build's ``check`` once the budget is spent."""


def families_of(inputs: EvalInputs) -> dict[str, frozenset[str]]:
    """Family -> the fingerprints of its records (the held-out refit's
    second filter, :mod:`.heldout`)."""
    family_of = {(g.campaign_id, g.group_id): g.family for g in inputs.groups}
    out: dict[str, set[str]] = {g.family: set() for g in inputs.groups}
    for record in inputs.records:
        family = family_of.get((record.campaign_id, record.group_id))
        if family is not None and record.fingerprint is not None:
            out[family].add(record.fingerprint)
    return {family: frozenset(prints) for family, prints in out.items()}


class ClassifierExamplesService:
    """See module docstring."""

    def __init__(
        self,
        engine: AsyncEngine,
        calibrations: CalibrationStore,
        calibration_config: CalibrationConfig,
        *,
        max_rows: int,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._calibrations = calibrations
        self._calibration_config = calibration_config
        self._max_rows = max_rows
        self._timeout_s = timeout_s
        self._monotonic = monotonic
        self._lock = asyncio.Lock()

    def _expired(self, phase: str, ids: Sequence[str]) -> ClassifierTimeoutError:
        log.warning(
            "controller.planner.classifier_examples_timeout",
            campaign_ids=list(ids),
            phase=phase,
            timeout_s=self._timeout_s,
        )
        what = (
            "another classifier examples build is still running"
            if phase == "lock_wait"
            else "the classifier examples build did not finish"
        )
        return ClassifierTimeoutError(
            f"{what} after {self._timeout_s:g} s "
            "([controller.planner] ml_classifier_timeout_s); retry later or "
            "use fewer campaigns",
            details={"phase": phase, "timeout_s": self._timeout_s},
        )

    async def document(
        self, campaign_ids: Sequence[str], *, user_id: str | None = None
    ) -> dict[str, Any]:
        """The examples document. Raises :class:`~.service.EvalNotFoundError`,
        :class:`~.loader.EvalTooLargeError`, :class:`~.loader.EvalLoadError`,
        :class:`~meshforge.controller.calibration_store.CalibrationStoreError`,
        :class:`ClassifierTimeoutError` (module docstring)."""
        started = self._monotonic()
        deadline = started + self._timeout_s
        ids = sorted(set(campaign_ids))

        def left() -> float:
            return max(deadline - self._monotonic(), 0.0)

        def check() -> None:
            if self._monotonic() >= deadline:
                raise _ExpiredError

        try:
            await asyncio.wait_for(self._lock.acquire(), left())
        except TimeoutError:
            raise self._expired("lock_wait", ids) from None
        try:
            try:
                async with asyncio.timeout(left()):
                    missing = await missing_campaigns(self._engine, ids)
                    if missing:
                        raise EvalNotFoundError(
                            "no such campaign: " + ", ".join(missing),
                            details={"campaign_ids": missing},
                        )
                    inputs = await load_campaigns(
                        self._engine, ids, max_rows=self._max_rows
                    )
                    runs = await self._calibrations.matched_runs(
                        self._calibration_config
                    )
            except TimeoutError:
                raise self._expired("load", ids) from None
            families = families_of(inputs)
            # the threads are awaited to the end (never abandoned): the
            # refit is one fit per family, the build checks before every group
            choices = await asyncio.to_thread(
                refit_choices, runs, families, self._calibration_config
            )
            try:
                check()
                examples, counts = await asyncio.to_thread(
                    build_examples,
                    inputs.groups,
                    inputs.records,
                    choices,
                    check=check,
                )
            except _ExpiredError:
                raise self._expired("build", ids) from None
        finally:
            self._lock.release()
        log.info(
            "controller.planner.classifier_examples",
            user_id=user_id,
            campaign_ids=ids,
            groups=counts.get("groups", 0),
            examples=len(examples),
            families=len({e.family for e in examples}),
            took_s=round(self._monotonic() - started, 3),
        )
        return examples_document(
            examples, counts, campaign_ids=ids, calibrations=choices
        )
