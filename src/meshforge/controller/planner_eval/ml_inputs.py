"""What the evaluator's ``cost_model:ml`` rows plan with (V0.15 G5a,
D368/D369). I/O: the dataset, the training child, the registry.

:meth:`FoldMlInputs.prepare` answers one :class:`~.model.EvalMl` per report:

* ``[controller.planner] ml = off`` or the ``ml`` extra missing ->
  ``ml_unavailable`` (the report has no ML rows; its ML verdicts say
  ``ml_unavailable``, never ``FAIL``);
* otherwise one **fold model per evaluated family** (D368): the G1 dataset is
  built once (the same builder as the retrain), written to a private
  temporary directory, and for each family ``F`` (sorted) the training child
  trains a set on it without ``F`` - without ``F``'s rows and without the
  rows of the workload groups ``F``'s evaluated records belong to (the
  calibration refit's rule, D316) - with the report's start as the cut-off
  (:mod:`meshforge.controller.ml.folds`). Sequential, each under
  ``ml_retrain_timeout_s``, each holding the controller-wide training-child
  guard (never next to a retrain), all together under
  ``ml_eval_timeout_s`` (G5a reviewer B2): a fold still waiting or training
  when it is spent is stopped (its child killed) and every family left gets
  no model, reason ``eval_budget_exhausted``. A family whose fold has no
  usable model plans with calibration only (``no_fold_model``), with the
  reason in its ``folds`` entry;
* ``allow_leak`` (debugging): the **serving** set (pinned, else active)
  for every family, loaded by version through the registry loader
  (:class:`~meshforge.controller.ml.registry_loader.RegistryLoader`) - the
  report is stamped ``leaked`` anyway.

The fold sets are never stored or registered.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from meshforge.common.logging import get_logger
from meshforge.controller.ml.child import (
    ChildRunner,
    ml_missing,
    run_child,
    scratch_dir,
)
from meshforge.controller.ml.export import BuiltDataset
from meshforge.controller.ml.folds import (
    FoldOutcome,
    budget_exhausted,
    pool_terms,
    train_fold,
)
from meshforge.controller.ml.loader import MlLoadError
from meshforge.controller.ml.registry import MlRegistryError
from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.planner_eval.loader import EvalLoadError
from meshforge.controller.planner_eval.model import ML_EVALUATED, ML_UNAVAILABLE, EvalMl
from meshforge.controller.settings import PlannerSettings

__all__ = ["EvalMlInputs", "FoldMlInputs"]

log = get_logger(__name__)


class EvalMlInputs(Protocol):
    """What :class:`~.service.EvalService` asks for the ML rows."""

    async def prepare(
        self, families: Mapping[str, frozenset[str]], *, allow_leak: bool
    ) -> EvalMl:
        """``families`` = evaluated family -> its evaluated records'
        fingerprints."""
        ...


class FoldMlInputs:
    """See module docstring."""

    def __init__(
        self,
        settings: PlannerSettings,
        models: ModelSource,
        loader: RegistryLoader,
        build: Callable[[], Awaitable[BuiltDataset]],
        *,
        python: str = sys.executable,
        child: ChildRunner = run_child,
        missing: Callable[[], list[str]] = ml_missing,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._models = models
        self._loader = loader
        self._build = build
        self._python = python
        self._child = child
        self._missing = missing
        self._clock = clock
        self._monotonic = monotonic

    async def prepare(
        self, families: Mapping[str, frozenset[str]], *, allow_leak: bool
    ) -> EvalMl:
        """The ML rows' inputs (module docstring). Raises
        :class:`~.loader.EvalLoadError` when the database fails."""
        if self._models.mode == "off":
            return EvalMl(ML_UNAVAILABLE, "[controller.planner] ml = off")
        missing = self._missing()
        if missing:
            return EvalMl(
                ML_UNAVAILABLE,
                "the ml extra is not installed on the controller (missing: "
                + ", ".join(missing)
                + ")",
            )
        try:
            if allow_leak:
                return await self._serving(sorted(families))
            return await self._folds(families)
        except (MlLoadError, MlRegistryError) as exc:
            raise EvalLoadError(
                "the evaluator's ML read failed in the database",
                details={"cause": exc.details.get("cause")},
            ) from exc

    async def _serving(self, families: list[str]) -> EvalMl:
        kind, version = await self._models.serving_version()
        ml = None if version is None else await self._loader.load(version)
        reason = None
        if version is None:
            reason = "no model set serves"
        elif ml is None:
            reason = "the serving set cannot be loaded"
        folds: list[dict[str, Any]] = [
            {
                "family": family,
                "source": "active" if ml is not None else "none",
                "set_version": version,
                "reason": reason,
                "train_rows": None,
                "trained_families": [],
                "excluded_rows": 0,
                "learned_terms": [],
                "until": None,
            }
            for family in families
        ]
        log.info(
            "controller.planner.eval_ml_serving",
            ml_set_version=version,
            kind=kind,
            loaded=ml is not None,
        )
        return EvalMl(
            ML_EVALUATED,
            "allow_leak: the serving set for every family",
            inputs=dict.fromkeys(families, ml),
            folds=folds,
        )

    async def _folds(self, families: Mapping[str, frozenset[str]]) -> EvalMl:
        until = self._clock()
        budget_s = self._settings.ml_eval_timeout_s
        deadline = self._monotonic() + budget_s
        built = await self._build()
        outcomes: list[FoldOutcome] = []
        async with scratch_dir("meshforge-ml-eval-") as root:
            dataset = root / "dataset.parquet"
            await asyncio.to_thread(dataset.write_bytes, built.dataset.parquet)
            for index, family in enumerate(sorted(families)):
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    outcome = budget_exhausted(family, until, budget_s, started=False)
                else:
                    outcome = await self._fold(
                        root / f"fold-{index}",
                        dataset,
                        family,
                        families[family],
                        until,
                        remaining,
                    )
                log.info(
                    "controller.planner.eval_ml_fold",
                    family=family,
                    ml_set_version=outcome.entry.get("set_version"),
                    learned_terms=outcome.entry.get("learned_terms"),
                    reason=outcome.entry.get("reason"),
                )
                outcomes.append(outcome)
        return EvalMl(
            ML_EVALUATED,
            None,
            inputs={o.family: o.ml for o in outcomes},
            folds=[o.entry for o in outcomes],
            terms=pool_terms(
                outcomes, max_interval_log=self._settings.ml_max_interval_log
            ),
            until=until.isoformat(),
        )

    async def _fold(
        self,
        work: Path,
        dataset: Path,
        family: str,
        fingerprints: frozenset[str],
        until: dt.datetime,
        remaining_s: float,
    ) -> FoldOutcome:
        """One fold, the wait for the training guard and the child both
        bounded by what is left of ``ml_eval_timeout_s``."""
        try:
            async with asyncio.timeout(remaining_s):
                await asyncio.to_thread(work.mkdir)
                return await train_fold(
                    python=self._python,
                    dataset=dataset,
                    work=work,
                    family=family,
                    fingerprints=fingerprints,
                    until=until,
                    settings=self._settings,
                    child=self._child,
                    timeout_s=min(self._settings.ml_retrain_timeout_s, remaining_s),
                )
        except TimeoutError:
            # the budget ran out mid-fold: the cancel killed the child
            return budget_exhausted(
                family, until, self._settings.ml_eval_timeout_s, started=True
            )
