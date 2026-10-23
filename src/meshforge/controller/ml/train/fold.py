"""One evaluator fold model (V0.15 G5a, D368) - the child side of
``python -m meshforge.controller.ml.train fit --exclude-family F``.

The planner evaluator judges ML on a held-out workload family ``F`` with a
model set that never saw ``F``, built **exactly** the way the G3 retrain
builds the set that ships (:func:`~.fit.train_set`: same floors, same
selection, same artefacts) - only on fewer rows:

* :func:`split_rows` - the rows that finished by ``until`` (the report's
  cut-off), minus every row of ``F`` **and** every row of a workload group
  (fingerprint) ``F``'s rows or the evaluator's records belong to: a user job
  of the same workload carries no family, but it is the same workload (the
  calibration refit's rule, D316). ``F``'s own rows are the held-out rows.
* :func:`heldout_predictions` - every learned member, loaded from its
  artefact bytes through the **serving** path (:func:`~..serve.load_model`,
  the controller's own loader), predicts the held-out rows: per term the
  rows' ``y``, ``raw_s``, ``actual_s``, ``y_hat``, the conformal half-width
  ``q`` and the OOD flag (``heldout.json``; the controller pools the folds,
  :mod:`meshforge.controller.ml.folds`).

Nothing here gates or registers: a fold set is never activatable.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from meshforge.controller.ml.serve import load_model
from meshforge.controller.ml.train.data import Table, table_from_rows
from meshforge.controller.ml.train.fit import TrainResult

__all__ = [
    "FOLD_DOC_VERSION",
    "HELDOUT_FILE",
    "FoldSplit",
    "fold_document",
    "held_table",
    "heldout_predictions",
    "split_rows",
]

FOLD_DOC_VERSION: Final = 1
HELDOUT_FILE: Final = "heldout.json"


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class FoldSplit:
    family: str
    until: dt.datetime | None
    train: list[Mapping[str, Any]]
    """The rows the fold set is trained on (no row of ``family``, none of
    an excluded group, none after ``until``)."""
    held: list[Mapping[str, Any]]
    """``family``'s rows (finished by ``until``)."""
    excluded_rows: int
    """Rows left out of training for being ``family``'s or of an excluded
    group (``held`` included); rows after ``until`` are not counted here."""
    late_rows: int
    """Rows that finished after ``until``."""
    excluded_groups: int


def split_rows(
    rows: Iterable[Mapping[str, Any]],
    family: str,
    *,
    until: dt.datetime | None,
    groups: Iterable[str] = (),
) -> FoldSplit:
    """The fold's training and held-out rows (module docstring)."""
    cut = None if until is None else _utc(until)
    timely: list[Mapping[str, Any]] = []
    late = 0
    for row in rows:
        if cut is not None and _utc(row["finished_at"]) > cut:
            late += 1
        else:
            timely.append(row)
    held = [row for row in timely if row["family"] == family]
    excluded = {str(row["group"]) for row in held} | {str(g) for g in groups}
    train = [
        row
        for row in timely
        if row["family"] != family and str(row["group"]) not in excluded
    ]
    return FoldSplit(
        family=family,
        until=cut,
        train=train,
        held=held,
        excluded_rows=len(timely) - len(train),
        late_rows=late,
        excluded_groups=len(excluded),
    )


def _floats(values: Any) -> list[float | None]:
    return [
        value if math.isfinite(value) else None
        for value in np.asarray(values, dtype=np.float64).tolist()
    ]


def heldout_predictions(
    result: TrainResult, held: Table | None
) -> dict[str, dict[str, list[Any]]]:
    """Per learned term, the held-out rows and the member's answers (module
    docstring). Terms without a learned member or without held-out rows are
    absent."""
    out: dict[str, dict[str, list[Any]]] = {}
    if held is None or held.n == 0:
        return out
    for member in result.members:
        if member.artefact is None:
            continue
        rows = held.rows_of(member.term)
        if rows.size == 0:
            continue
        model = load_model(member.artefact, expected_version=member.version)
        answer = model.predict(held.X[rows])
        y_hat = np.asarray(answer.y_hat, dtype=np.float64)
        q = (np.asarray(answer.hi) - np.asarray(answer.lo)) / 2.0
        out[member.term] = {
            "y": _floats(held.y[rows]),
            "raw_s": _floats(held.raw_s[rows]),
            "actual_s": _floats(held.actual_s[rows]),
            "y_hat": _floats(y_hat),
            "q": _floats(q),
            "ood": [bool(flag) for flag in np.asarray(answer.ood).tolist()],
        }
    return out


def held_table(split: FoldSplit) -> Table | None:
    return table_from_rows(split.held) if split.held else None


def fold_document(split: FoldSplit, result: TrainResult) -> dict[str, Any]:
    """``set.json``'s ``fold`` block: what the fold trained on (the leak
    test reads ``trained_families``)."""
    families: Sequence[str] = sorted(
        {str(row["family"]) for row in split.train if row["family"]}
    )
    return {
        "fold_doc_version": FOLD_DOC_VERSION,
        "family": split.family,
        "until": None if split.until is None else split.until.isoformat(),
        "train_rows": len(split.train),
        "heldout_rows": len(split.held),
        "excluded_rows": split.excluded_rows,
        "late_rows": split.late_rows,
        "excluded_groups": split.excluded_groups,
        "trained_families": list(families),
        "learned_terms": sorted(m.term for m in result.members if m.artefact),
    }
