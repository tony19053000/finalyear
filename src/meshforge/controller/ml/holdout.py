"""The activation gate's held-out slice of a dataset (V0.15 G3 D339 via
D329; shared with the G5a regret check, D371). Pure: no numpy, no database.

The slice is the calibration's own D329 split
(:func:`~meshforge.controller.planner.calibration.split_holdout`, never a
copy): per workload fingerprint the most recent 20 % of its decisions
(one-run fingerprints share a pool). Every row of a decision is on one side.

:func:`held_out_decisions` takes one ``(decision_id, node_id, finished_at,
family, group)`` tuple per dataset row - the G3 error gate passes its
table's columns (:func:`~.train.gate.holdout_mask`), the controller's
regret check (D371) the dataset rows it built - so the two checks of one
retrain judge **the same** decisions.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from meshforge.controller.planner.calibration import (
    HOLDOUT_FRAC,
    MatchedRun,
    split_holdout,
)

__all__ = ["DecisionRow", "held_out_decisions", "rows_of_dataset"]

type DecisionRow = tuple[int, str, dt.datetime, str | None, str]
"""``(decision_id, node_id, finished_at, family or None, group)``."""


def held_out_decisions(
    rows: Iterable[DecisionRow],
) -> tuple[frozenset[int], dict[str, Any]]:
    """The held-out decision ids (module docstring) and the split's counts.
    A decision's newest row names its run; its fingerprint is its first
    row's group (all rows of a decision share it)."""
    runs: dict[int, MatchedRun] = {}
    for decision, node_id, finished, family, group in rows:
        known = runs.get(decision)
        if known is None or finished > known.finished_at:
            runs[decision] = MatchedRun(
                decision_id=decision,
                attempt_id=str(decision),
                node_id=node_id,
                finished_at=finished,
                terms=(),
                family=family,
                fingerprint=known.fingerprint if known else group,
            )
    _fit, held = split_holdout(runs.values())
    held_ids = frozenset(run.decision_id for run in held)
    return held_ids, {
        "decisions": len(runs),
        "decisions_held": len(held_ids),
        "holdout_frac": HOLDOUT_FRAC,
        "split": "D329 stratified by fingerprint",
    }


def rows_of_dataset(rows: Iterable[Mapping[str, Any]]) -> list[DecisionRow]:
    """G1 dataset rows (``ml.dataset`` column mappings) as
    :data:`DecisionRow` s, in the dataset's own order."""
    return [
        (
            int(row["decision_id"]),
            str(row["node_id"]),
            row["finished_at"],
            str(row["family"]) if row["family"] else None,
            str(row["group"]),
        )
        for row in rows
    ]
