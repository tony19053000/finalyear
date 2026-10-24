"""Predicted vs actual per term, for the Models page scatter (V0.15 G5b,
D374).

For the newest ``limit`` **matched runs** (the calibration's own read,
:func:`~meshforge.controller.calibration_store.read_outcomes`: ``dispatched``
decisions whose attempt ended ``done``, newest first) each learnable term
that has an honest actual becomes one :class:`PredictionPoint` - matched
exactly as the ML dataset matches it (:func:`~.targets.observations`: a
whole job's or one-lane split's terms, a Mode 3 set's ``lane_stage`` /
``lane_env`` / ``chunk_rate`` per lane and its ``combine``; skips such as
exact-by-construction terms give no point). Everything comes from the stored
decision JSON - no migration (D374, Q29):

* ``analytical_s`` - the term's raw (pre-calibration, pre-ML) prediction, the
  dataset's ``raw_s``;
* ``ml_s`` - ``analytical_s x e^y_hat`` when ML corrected the term
  (``detail.ml.used``), else null;
* ``calibrated_s`` - what the **calibration** predicted, or would have: the
  stored factor when the term was calibrated; for a term ML corrected (the
  calibration was then not applied) the decision's own calibration version
  re-applied with the planner's :func:`~meshforge.controller.planner.
  calibration.calibrate_term` (null if that version cannot be read); the
  analytical value for a term never calibrated (``chunk_rate``,
  ``combine``, a ``measured`` source, an uncalibrated decision);
* ``used`` / ``reason`` / ``ml_model_version`` - from ``detail.ml`` and the
  row's ``ml_model_version`` (null for a decision planned without a model).

A ``chunk_rate`` is in seconds per item (its block is the one-lane row's
``compute`` ``detail.ml.rate``, G4 fix r1).

:func:`read_predictions` is the I/O; :func:`points_of` is pure.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.controller.calibration_store import (
    CalibrationStore,
    CalibrationStoreError,
    read_outcomes,
)
from meshforge.controller.ml.loader import read_runs
from meshforge.controller.ml.targets import Observation, RunInput, observations
from meshforge.controller.planner.calibration import Calibration, calibrate_term
from meshforge.controller.planner.estimator import bounded
from meshforge.controller.planner.predictor import MAX_Y_HAT
from meshforge.controller.planner.types import EstimateTerm, TermSource
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.plan import PlanDecision

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_CALIBRATION_VERSIONS",
    "MAX_LIMIT",
    "PredictionPoint",
    "PredictionsError",
    "PredictionsRead",
    "calibration_skips",
    "points_of",
    "read_predictions",
]

DEFAULT_LIMIT: Final = 500
MAX_LIMIT: Final = 2000
"""D374: at most this many decisions per request (the route's bound)."""
MAX_CALIBRATION_VERSIONS: Final = 16
"""Distinct calibration versions read per request (each is one row); the
points of decisions with any other version get ``calibrated_s`` null when ML
corrected them - counted in :attr:`PredictionsRead.calibration_skipped_points`
(V0.15 G6b) so the Models tab can say so."""

_CALIBRATED_AS: Final[Mapping[str, str]] = {
    "transfer": "transfer",
    "env": "env",
    "compute": "compute",
    "output": "output",
    "lane_stage": "transfer",
    "lane_env": "env",
}
"""ML term -> the calibration's term (``chunk_rate`` / ``combine``: never
calibrated, V0.14)."""
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)


class PredictionsError(MeshForgeError):
    """The database refused or lost the read (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class PredictionPoint:
    decision_id: int
    term: str
    node_id: str
    family: str | None
    analytical_s: float
    calibrated_s: float | None
    ml_s: float | None
    used: bool
    reason: str | None
    actual_s: float
    ml_model_version: str | None


@dataclass(frozen=True, slots=True)
class PredictionsRead:
    points: list[PredictionPoint]
    decisions_read: int
    calibration_versions_skipped: int = 0
    """V0.15 G6b (G5 debt): distinct calibration versions of the read
    decisions older than the newest :data:`MAX_CALIBRATION_VERSIONS` - not
    read, so an ML-corrected term of such a decision has ``calibrated_s``
    null."""
    calibration_skipped_points: int = 0
    """Points whose ``calibrated_s`` is null because their decision's
    calibration version was one of those skipped (the rest of the null
    ``calibrated_s`` are versions that cannot be read)."""


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _chosen(run: RunInput) -> Mapping[str, Any] | None:
    for row in run.outcome.candidates:
        if (
            isinstance(row, Mapping)
            and row.get("rank") == 1
            and row.get("node_id") == run.outcome.chosen_node_id
        ):
            return row
    return None


def _lane_rate_block(run: RunInput, node_id: str) -> Mapping[str, Any] | None:
    """The ``chunk_rate``'s block: the node's one-lane row's ``compute``
    ``detail.ml.rate`` (the row the lane was costed with)."""
    for row in run.outcome.candidates:
        if not isinstance(row, Mapping) or row.get("mode") == "split":
            continue
        if row.get("node_set") != [node_id]:
            continue
        terms = (row.get("estimate") or {}).get("terms")
        compute = terms.get("compute") if isinstance(terms, Mapping) else None
        detail = compute.get("detail") if isinstance(compute, Mapping) else None
        outer = detail.get("ml") if isinstance(detail, Mapping) else None
        rate = outer.get("rate") if isinstance(outer, Mapping) else None
        return rate if isinstance(rate, Mapping) else None
    return None


def _block(run: RunInput, obs: Observation) -> Mapping[str, Any] | None:
    if obs.term == "chunk_rate":
        return _lane_rate_block(run, obs.node_id)
    block = obs.detail.get("ml")
    return block if isinstance(block, Mapping) else None


def _calibrated(
    obs: Observation,
    *,
    used: bool,
    run: RunInput,
    version: int | None,
    calibrations: Mapping[int, Calibration | None],
) -> float | None:
    name = _CALIBRATED_AS.get(obs.term)
    if name is None:
        return obs.raw_s
    stored = obs.detail.get("calibration")
    factor = _num(stored.get("factor")) if isinstance(stored, Mapping) else None
    if factor is not None:
        return bounded(obs.raw_s * factor)
    if not used or version is None:
        return obs.raw_s  # not scaled: uncalibrated, measured, or no factor
    calibration = calibrations.get(version)
    if calibration is None:
        return None  # the version the decision used cannot be read
    try:
        source = TermSource(obs.source or "")
    except ValueError:
        return None
    term = calibrate_term(
        name,
        EstimateTerm(obs.raw_s, source, {}),
        calibration,
        node_id=obs.node_id,
        family=calibration.family_of(run.outcome.fingerprint),
    )
    return term.seconds


def _point(
    run: RunInput,
    obs: Observation,
    *,
    set_version: str | None,
    version: int | None,
    calibrations: Mapping[int, Calibration | None],
) -> PredictionPoint:
    block = _block(run, obs)
    used = block is not None and block.get("used") is True
    y_hat = None if block is None else _num(block.get("y_hat"))
    ml_s = (
        obs.raw_s * math.exp(y_hat)
        if used and y_hat is not None and abs(y_hat) <= MAX_Y_HAT
        else None
    )
    return PredictionPoint(
        decision_id=run.outcome.decision_id,
        term=obs.term,
        node_id=obs.node_id,
        family=run.outcome.family,
        analytical_s=obs.raw_s,
        calibrated_s=_calibrated(
            obs, used=used, run=run, version=version, calibrations=calibrations
        ),
        ml_s=ml_s,
        used=used,
        reason=None if block is None or used else _text(block.get("reason")),
        actual_s=obs.actual_s,
        ml_model_version=set_version,
    )


def points_of(
    runs: Sequence[RunInput],
    *,
    versions: Mapping[int, int | None],
    calibrations: Mapping[int, Calibration | None],
    term: str | None = None,
    set_version: str | None = None,
) -> list[PredictionPoint]:
    """The points of ``runs`` in order (module docstring). ``versions`` =
    decision id -> its calibration version; ``term`` / ``set_version``
    filter (``set_version`` = the model set the decision was planned
    with)."""
    out: list[PredictionPoint] = []
    for run in runs:
        chosen = _chosen(run)
        planned_with = None if chosen is None else _text(chosen.get("ml_model_version"))
        if set_version is not None and planned_with != set_version:
            continue
        rows, _skipped = observations(run)
        for obs in rows:
            if term is not None and obs.term != term:
                continue
            out.append(
                _point(
                    run,
                    obs,
                    set_version=planned_with,
                    version=versions.get(run.outcome.decision_id),
                    calibrations=calibrations,
                )
            )
    return out


async def read_predictions(
    engine: AsyncEngine,
    calibration_store: CalibrationStore,
    *,
    limit: int = DEFAULT_LIMIT,
    term: str | None = None,
    set_version: str | None = None,
) -> PredictionsRead:
    """The points of the newest ``limit`` matched runs (``limit`` clamped to
    ``[1, MAX_LIMIT]``); :class:`PredictionsError` when the database
    fails. Bounded: one keyset-free ``LIMIT`` read plus ``IN`` reads, no
    replay is loaded."""
    bound = max(1, min(int(limit), MAX_LIMIT))
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db:
            outcomes = await read_outcomes(db, bound)
            runs = await read_runs(db, outcomes, with_replay=False)
            ids = [o.decision_id for o in outcomes]
            versions: dict[int, int | None] = {}
            if ids:
                for decision_id, version in await db.execute(
                    select(PlanDecision.id, PlanDecision.calibration_version).where(
                        PlanDecision.id.in_(ids)
                    )
                ):
                    versions[int(decision_id)] = (
                        None if version is None else int(version)
                    )
    except _DB_ERRORS as exc:
        raise PredictionsError(
            "the predictions read failed in the database",
            details={"cause": type(exc).__name__},
        ) from exc
    wanted = sorted({v for v in versions.values() if v is not None}, reverse=True)
    calibrations: dict[int, Calibration | None] = {}
    try:
        for version in wanted[:MAX_CALIBRATION_VERSIONS]:
            calibrations[version] = await calibration_store.load(version)
    except CalibrationStoreError as exc:
        raise PredictionsError(
            "the calibration read failed in the database",
            details={"cause": exc.details.get("cause", type(exc).__name__)},
        ) from exc
    points = points_of(
        runs,
        versions=versions,
        calibrations=calibrations,
        term=term,
        set_version=set_version,
    )
    skipped_versions, skipped_points = calibration_skips(points, versions)
    return PredictionsRead(
        points=points,
        decisions_read=len(outcomes),
        calibration_versions_skipped=skipped_versions,
        calibration_skipped_points=skipped_points,
    )


def calibration_skips(
    points: Sequence[PredictionPoint], versions: Mapping[int, int | None]
) -> tuple[int, int]:
    """V0.15 G6b (G5 debt): ``(versions, points)`` - how many distinct
    calibration versions of the read decisions are older than the newest
    :data:`MAX_CALIBRATION_VERSIONS` (never read), and how many points have
    ``calibrated_s`` null because of it."""
    wanted = sorted({v for v in versions.values() if v is not None}, reverse=True)
    skipped = set(wanted[MAX_CALIBRATION_VERSIONS:])
    points_skipped = sum(
        1
        for p in points
        if p.calibrated_s is None and versions.get(p.decision_id) in skipped
    )
    return len(skipped), points_skipped
