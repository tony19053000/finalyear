"""Held-out calibrations for the evaluator (D299 made concrete by D316).

Criterion 1 must never be measured on data the calibration was fitted on. So
the evaluator does **not** use the live active calibration for its calibrated
rows: for every workload family ``F`` it evaluates, it re-fits one with G2's own
:func:`~meshforge.controller.planner.calibration.fit` (no second
implementation) on the matched runs that are **not** ``F``:

* a run whose workload family is ``F`` is removed;
* so is a run whose workload fingerprint is one of ``F``'s evaluated records'
  fingerprints (a user job of the same workload carries no family, but it is
  the same workload - leaving it in would leak ``F`` through the back door).

``allow_leak`` (debugging) uses the active version for every family instead;
the report is then stamped ``leaked`` (:mod:`.core`).

A refit with no factor at all plans uncalibrated (``source = "none"``).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from meshforge.controller.planner.calibration import (
    Calibration,
    CalibrationConfig,
    MatchedRun,
    calibration_json,
    canonical,
    fit,
)
from meshforge.controller.planner_eval.model import check_cancel

__all__ = [
    "CalibrationChoice",
    "calibration_digest",
    "held_out",
    "leaked_choices",
    "refit_choices",
]


@dataclass(frozen=True, slots=True)
class CalibrationChoice:
    """The calibration one family's calibrated rows plan with."""

    family: str
    calibration: Calibration | None
    source: Literal["refit", "active", "none"]
    version: int | None
    fitted_runs: int
    excluded_runs: int


def calibration_digest(calibration: Calibration | None) -> str | None:
    """sha256 of the factors' canonical JSON (None for no / an empty one)."""
    if calibration is None or calibration.factor_count == 0:
        return None
    text = canonical(calibration_json(calibration))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def held_out(
    runs: Iterable[MatchedRun], family: str, fingerprints: frozenset[str]
) -> tuple[list[MatchedRun], list[MatchedRun]]:
    """``(kept, removed)``: the module docstring's filter for ``family``."""
    kept: list[MatchedRun] = []
    removed: list[MatchedRun] = []
    for run in runs:
        leaks = run.family == family or (
            run.fingerprint is not None and run.fingerprint in fingerprints
        )
        (removed if leaks else kept).append(run)
    return kept, removed


def refit_choices(
    runs: Sequence[MatchedRun],
    families: Mapping[str, frozenset[str]],
    config: CalibrationConfig,
    *,
    cancel: threading.Event | None = None,
) -> dict[str, CalibrationChoice]:
    """One leave-one-family-out refit per family (``family -> the
    fingerprints of its evaluated records``); ``cancel`` set -> stop before
    the next family (:class:`~.model.EvalCancelledError`)."""
    out: dict[str, CalibrationChoice] = {}
    for family in sorted(families):
        check_cancel(cancel)
        kept, removed = held_out(runs, family, families[family])
        fitted = fit(kept, config)
        usable = fitted if fitted.factor_count > 0 else None
        out[family] = CalibrationChoice(
            family=family,
            calibration=usable,
            source="refit" if usable is not None else "none",
            version=None,
            fitted_runs=len(kept),
            excluded_runs=len(removed),
        )
    return out


def leaked_choices(
    families: Iterable[str], active: Calibration | None
) -> dict[str, CalibrationChoice]:
    """``allow_leak``: the active version for every family."""
    return {
        family: CalibrationChoice(
            family=family,
            calibration=active,
            source="active" if active is not None else "none",
            version=None if active is None else active.version,
            fitted_runs=0,
            excluded_runs=0,
        )
        for family in sorted(set(families))
    }
