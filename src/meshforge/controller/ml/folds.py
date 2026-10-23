"""The planner evaluator's fold models, seen from the controller (V0.15 G5a,
D368).

For each evaluated workload family ``F`` the evaluator needs a model set
that never saw ``F``, built by the production pipeline and used through the
production serving path:

1. :func:`fold_argv` - ``python -m meshforge.controller.ml.train fit
   --exclude-family F --until TS [--exclude-groups FILE]`` (the retrain's
   own child, :mod:`.child`: ``nice``d, one at a time, each under
   ``ml_retrain_timeout_s``; the same floors and selection settings as the
   retrain, :func:`train_settings_argv`);
2. :func:`read_fold` - its ``set.json`` / artefacts / ``heldout.json``;
3. :func:`loaded_set` - the artefacts decoded by :func:`~.serve.load_model`
   (numpy, + LightGBM / XGBoost for those models; never scikit-learn, never
   the training package) into a :class:`~.serve.LoadedSet`, then
   :class:`~.source.SetPredictor` and :func:`~.predictor.ml_input` - exactly
   what the dispatcher plans with, minus the live time budget (offline);
4. :func:`pool_terms` - the folds' held-out rows pooled per term (MALE,
   MAPE, coverage, OOD and usable rate), pure Python.

A fold that has no learned member, fails, times out or cannot be read gives
``None`` for its family (the evaluator's ML rows plan that family with
calibration only and count it ``no_fold_model``) and says why in its
``entry``. Fold sets are written to a temporary directory and never stored.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.child import (
    TRAIN_MODULE,
    ChildOutputError,
    ChildRunner,
    describe,
    niced,
    read_output,
)
from meshforge.controller.ml.predictor import ml_input
from meshforge.controller.ml.serve import LoadedModel, LoadedSet, load_model
from meshforge.controller.ml.source import SetPredictor
from meshforge.controller.planner.predictor import MlInput
from meshforge.controller.settings import PlannerSettings

__all__ = [
    "HELDOUT_FILE",
    "FoldOutcome",
    "fold_argv",
    "loaded_set",
    "pool_terms",
    "read_fold",
    "train_fold",
    "train_settings_argv",
]

HELDOUT_FILE: Final = "heldout.json"
"""The fold child's held-out predictions (``ml.train.fold.HELDOUT_FILE``)."""
_ROUND: Final = 6


def train_settings_argv(settings: PlannerSettings) -> list[str]:
    """The training settings the retrain passes its child (the floors and
    the interval limit) - a fold uses the same, so it is built the way
    production builds."""
    return [
        "--min-rows",
        str(settings.ml_min_rows),
        "--min-families",
        str(settings.ml_min_families),
        "--min-effect",
        repr(settings.ml_min_effect_log),
        "--max-interval-log",
        repr(settings.ml_max_interval_log),
        "--family-tolerance-log",
        repr(settings.ml_family_tolerance_log),
    ]


def fold_argv(
    python: str,
    dataset: Path,
    out: Path,
    family: str,
    until: dt.datetime,
    groups: Path | None,
    settings: PlannerSettings,
) -> list[str]:
    """The fold child's command line (``nice``d when available)."""
    argv = [
        python,
        "-m",
        TRAIN_MODULE,
        "fit",
        "--dataset",
        str(dataset),
        "--out",
        str(out),
        *train_settings_argv(settings),
        "--exclude-family",
        family,
        "--until",
        until.isoformat(),
    ]
    if groups is not None:
        argv += ["--exclude-groups", str(groups)]
    return niced(argv)


def loaded_set(doc: Mapping[str, Any], artefacts: Mapping[str, bytes]) -> LoadedSet:
    """A child's set (``set.json`` + artefact bytes) loaded through the
    serving loader; :class:`~.artefact.ArtefactError` when a learned member
    cannot be (a set serves whole or not at all, like the registry)."""
    models: dict[str, LoadedModel] = {}
    without: dict[str, str] = {}
    for member in doc.get("members") or ():
        term = str(member["term"])
        if member.get("algorithm") == "none":
            without[term] = str(member.get("reason") or "no_learned_model")
            continue
        version = str(member["version"])
        data = artefacts.get(version)
        if data is None:
            raise ArtefactError(
                f"the {term} artefact is missing",
                details={"reason": "artefact_missing", "term": term},
            )
        models[term] = load_model(data, expected_version=version)
    return LoadedSet(
        set_version=str(doc["set_version"]),
        feature_version=int(doc["feature_version"]),
        models=models,
        without_model=without,
    )


def planner_input(loaded: LoadedSet, max_interval_log: float) -> MlInput | None:
    """``loaded`` as the planner's input, offline (no time budget); None
    when it has no learned model or its warm-up fails."""
    if not loaded.models:
        return None
    return ml_input(
        SetPredictor(loaded), max_interval_log=max_interval_log, budget_ms=None
    )


@dataclass(frozen=True, slots=True)
class FoldOutcome:
    family: str
    ml: MlInput | None
    """None = no usable model for this family (``entry["reason"]``)."""
    entry: dict[str, Any]
    """The report's ``ml.folds[]`` entry (``source``, ``set_version``,
    ``reason``, ``train_rows``, ``trained_families``, ...)."""
    heldout: dict[str, dict[str, list[Any]]] = field(default_factory=dict)
    """Per term: the held-out rows and predictions (``heldout.json``)."""


def _entry(
    family: str,
    *,
    until: dt.datetime | None,
    source: str = "fold",
    set_version: str | None = None,
    reason: str | None = None,
    fold: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fold = fold or {}
    return {
        "family": family,
        "source": source,
        "set_version": set_version,
        "reason": reason,
        "train_rows": fold.get("train_rows"),
        "trained_families": list(fold.get("trained_families") or []),
        "excluded_rows": int(fold.get("excluded_rows") or 0),
        "learned_terms": list(fold.get("learned_terms") or []),
        "until": None if until is None else until.isoformat(),
    }


def read_fold(
    out: Path, family: str, until: dt.datetime, max_interval_log: float
) -> FoldOutcome:
    """The fold child's output as a :class:`FoldOutcome`;
    :class:`~.child.ChildOutputError` when it cannot be read."""
    doc, artefacts, _timings = read_output(out)
    try:
        fold = doc["fold"]
        if not isinstance(fold, dict) or fold.get("family") != family:
            raise ValueError("the fold block does not name this family")  # noqa: TRY301
        if family in (fold.get("trained_families") or ()):
            # never evaluate a family on a model that trained on it (D368)
            raise ValueError("the fold set trained on its held-out family")  # noqa: TRY301
        heldout = json.loads((out / HELDOUT_FILE).read_text(encoding="utf-8"))
        if not isinstance(heldout, dict):
            raise TypeError("heldout.json is not an object")  # noqa: TRY301
        loaded = loaded_set(doc, artefacts)
    except (OSError, KeyError, TypeError, ValueError, ArtefactError) as exc:
        raise ChildOutputError(describe(exc)) from exc
    version = str(doc["set_version"])
    ml = planner_input(loaded, max_interval_log)
    if not loaded.models:
        reason: str | None = "no_learned_model"
        if doc.get("status") == "insufficient_data":
            reason = "insufficient_data"
    else:
        reason = None if ml is not None else "warm_up_failed"
    return FoldOutcome(
        family=family,
        ml=ml,
        entry=_entry(
            family, until=until, set_version=version, reason=reason, fold=fold
        ),
        heldout={str(k): v for k, v in heldout.items() if isinstance(v, dict)},
    )


async def train_fold(
    *,
    python: str,
    dataset: Path,
    work: Path,
    family: str,
    fingerprints: Iterable[str],
    until: dt.datetime,
    settings: PlannerSettings,
    child: ChildRunner,
) -> FoldOutcome:
    """One fold (module docstring): never raises for a failed fold - the
    outcome has no model and says why (``timeout``, ``child_failed``,
    ``child_output_invalid``)."""
    out = work / "out"
    groups = sorted(set(fingerprints))
    groups_path: Path | None = None
    if groups:
        groups_path = work / "groups.json"
        groups_path.write_text(json.dumps(groups), encoding="utf-8")
    argv = fold_argv(python, dataset, out, family, until, groups_path, settings)
    try:
        code, stderr = await child(argv, settings.ml_retrain_timeout_s)
    except TimeoutError:
        return FoldOutcome(
            family,
            None,
            _entry(
                family,
                until=until,
                source="none",
                reason=f"timeout: killed after {settings.ml_retrain_timeout_s:g} s",
            ),
        )
    if code != 0:
        last = stderr.splitlines()[-1] if stderr else ""
        return FoldOutcome(
            family,
            None,
            _entry(
                family,
                until=until,
                source="none",
                reason=f"child_failed: exit {code}: {last}"[:500],
            ),
        )
    try:
        return read_fold(out, family, until, settings.ml_max_interval_log)
    except ChildOutputError as exc:
        return FoldOutcome(
            family,
            None,
            _entry(
                family,
                until=until,
                source="none",
                reason=f"child_output_invalid: {exc}"[:500],
            ),
        )


# ------------------------------------------------------------------ metrics


def _r(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, _ROUND)


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _ape(raw_s: float, actual_s: float, y_hat: float) -> float | None:
    try:
        predicted = raw_s * math.exp(y_hat)
    except OverflowError:
        return None
    if not math.isfinite(predicted) or actual_s <= 0.0:
        return None
    return abs(predicted - actual_s) / actual_s


def _rows(
    block: Mapping[str, Any],
) -> list[tuple[float, float, float, float, float, bool]]:
    """``(y, raw_s, actual_s, y_hat, q, ood)`` of one fold's term; rows with
    a missing number are skipped (an infinite ``q`` is kept as inf)."""
    columns: list[list[Any]] = []
    for key in ("y", "raw_s", "actual_s", "y_hat", "q", "ood"):
        column = block.get(key)
        if not isinstance(column, list):
            return []
        columns.append(column)
    if any(len(c) != len(columns[0]) for c in columns):
        return []
    out = []
    for y, raw, actual, y_hat, q, ood in zip(*columns, strict=True):
        if None in (y, raw, actual, y_hat):
            continue
        out.append(
            (
                float(y),
                float(raw),
                float(actual),
                float(y_hat),
                math.inf if q is None else float(q),
                bool(ood),
            )
        )
    return out


def pool_terms(
    outcomes: Iterable[FoldOutcome], *, max_interval_log: float
) -> dict[str, dict[str, Any]]:
    """Per term, the fold models' error on their held-out family's rows,
    every fold pooled (the G2 metrics, :mod:`~.train.metrics`, recomputed
    here without numpy): MALE / MAPE of the model and of the raw analytical
    value, median APE, conformal coverage, OOD rate and usable rate (in
    distribution and ``2 q <= max_interval_log``). ``families`` = folds
    that contributed rows."""
    rows: dict[str, list[tuple[float, float, float, float, float, bool]]] = {}
    families: dict[str, set[str]] = {}
    for outcome in outcomes:
        for term, block in outcome.heldout.items():
            got = _rows(block)
            if got:
                rows.setdefault(term, []).extend(got)
                families.setdefault(term, set()).add(outcome.family)
    out: dict[str, dict[str, Any]] = {}
    for term in sorted(rows):
        part = rows[term]
        ape_model = [
            a
            for y, raw, act, y_hat, _q, _o in part
            if (a := _ape(raw, act, y_hat)) is not None
        ]
        ape_raw = [
            a for _y, raw, act, *_ in part if (a := _ape(raw, act, 0.0)) is not None
        ]
        out[term] = {
            "rows": len(part),
            "families": len(families[term]),
            "male_model": _r(
                _mean([abs(y_hat - y) for y, _r_, _a, y_hat, _q, _o in part])
            ),
            "male_raw": _r(_mean([abs(y) for y, *_ in part])),
            "mape_model": _r(_mean(ape_model)),
            "mape_raw": _r(_mean(ape_raw)),
            "median_ape_model": _r(statistics.median(ape_model)) if ape_model else None,
            "coverage": _r(
                _mean(
                    [
                        1.0 if abs(y - y_hat) <= q else 0.0
                        for y, _r_, _a, y_hat, q, _o in part
                    ]
                )
            ),
            "ood_rate": _r(_mean([1.0 if ood else 0.0 for *_, ood in part])),
            "usable_rate": _r(
                _mean(
                    [
                        1.0 if (not ood and 2.0 * q <= max_interval_log) else 0.0
                        for *_, q, ood in part
                    ]
                )
            ),
        }
    return out
