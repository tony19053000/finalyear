"""The activation gate of a new model set (V0.15 G3, D339 + D360; criterion 2
proxy).

A trained set (:func:`~.fit.train_set` on **all** rows - what ships) goes
live only if, on a held-out slice of the **current** dataset, it is not worse
than what the planner uses now. As the calibration's D306/D319 gate:

1. **The slice** - the D329 stratified hold-out (the calibration's own
   :func:`~meshforge.controller.planner.calibration.split_holdout`, never a
   copy): per workload fingerprint the most recent 20 % of its decisions
   (one-run fingerprints share a pool). Every row of a decision is on one
   side.
2. **The judged models** - each shipped learned member's **configuration**
   refitted on the other rows (:func:`~.fit.train_set` ``fixed=``; its
   interval from grouped out-of-fold residuals of those rows, its OOD test
   fitted on them), **round-tripped through its artefact** and served by
   :mod:`..serve` - the gate judges exactly the code path the planner will
   use. (The configuration was chosen on all rows - a mild optimism, the
   price of judging the configuration that actually ships rather than a
   possibly different re-selection.)
3. **The reference** - what serves now: the pinned set, else the active set
   (its stored artefacts as they are, like D306 judges the active
   calibration as it is), else - nothing active, or a term the reference has
   no readable model for - the calibration refitted on the other rows (the
   live fallback, D350).
4. **Effective predictions** - what the planner will deliver (D336
   precedence): the model's ``y_hat`` where the row is in distribution and
   the interval width ``2q <= max_interval_log``; else the calibration's.

Rules, in this order (the first that fails is the set's ``reason``; every
rule's numbers are in the gate document either way):

* ``too_few_held_out`` - a shipped learned term with fewer than
  ``min_held_rows`` (20) held-out rows cannot be judged;
* ``coverage`` - a learned term's held-out conformal coverage < 80 %
  pooled, or < 70 % on a family with >= 20 held-out rows (D338 / D356);
* ``family_tolerance`` - on a family with >= ``family_min_rows`` (5)
  held-out rows, a learned term's effective log error is above the
  calibration's by more than ``family_tolerance_log`` (0.05) - criterion 2,
  in log error;
* ``worse_than_reference`` - a term's effective log error (MALE) above the
  reference's x 1.05 (D339; the calibration's ``GATE_TOLERANCE``);
* ``not_better`` - no learned term beats the calibration by more than
  ``min_effect_log`` (0.02, D357): a set that changes nothing useful is
  never activated (pure noise lands here).

**Regret** (the D339 regret part needs the planner's ``TermPredictor``,
V0.15 G4): recorded as ``"regret": "pending_g4"`` (D360); G5 adds the D319
regret check.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np

from meshforge.controller.ml.serve import LoadedModel, load_model
from meshforge.controller.ml.train.algorithms import Config, FitContext, fit_one
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import FloatArray, IntArray, Table
from meshforge.controller.ml.train.fit import TrainResult, train_set
from meshforge.controller.ml.train.report import clean
from meshforge.controller.planner.calibration import (
    GATE_TOLERANCE,
    HOLDOUT_FRAC,
    MatchedRun,
    split_holdout,
)

__all__ = [
    "GATE_RULES",
    "GateResult",
    "GateSettings",
    "Reference",
    "holdout_mask",
    "run_gate",
]

GATE_RULES: Final = (
    "too_few_held_out",
    "coverage",
    "family_tolerance",
    "worse_than_reference",
    "not_better",
)
REGRET_PENDING: Final = "pending_g4"
_CALIBRATION: Final = Config("calibration")
_EPS: Final = 1e-12


@dataclass(frozen=True, slots=True)
class GateSettings:
    tolerance: float = GATE_TOLERANCE
    min_held_rows: int = 20
    family_min_rows: int = 5


@dataclass(frozen=True, slots=True)
class Reference:
    """What serves now (module docstring, step 3)."""

    kind: str = "calibration"
    """``pinned`` | ``active`` | ``calibration`` (nothing serves)."""
    set_version: str | None = None
    models: Mapping[str, LoadedModel] = field(default_factory=dict)
    unreadable: Mapping[str, str] = field(default_factory=dict)
    """Term -> why its stored model could not be loaded (judged against the
    calibration instead)."""


@dataclass(frozen=True, slots=True)
class GateResult:
    accepted: bool
    rule: str | None
    reason: str | None
    doc: dict[str, Any]


def holdout_mask(table: Table) -> tuple[np.ndarray, dict[str, Any]]:
    """``held`` (bool per row) by the D329 split over the table's decisions
    (module docstring, step 1), and its counts."""
    runs: dict[int, MatchedRun] = {}
    for i in range(table.n):
        decision = table.decision_id[i]
        finished = table.finished_at[i]
        known = runs.get(decision)
        if known is None or finished > known.finished_at:
            runs[decision] = MatchedRun(
                decision_id=decision,
                attempt_id=str(decision),
                node_id=table.node_id[i],
                finished_at=finished,
                terms=(),
                family=table.family_raw[i],
                fingerprint=known.fingerprint if known else table.group[i],
            )
    _fit, held = split_holdout(runs.values())
    held_ids = {run.decision_id for run in held}
    mask = np.asarray([d in held_ids for d in table.decision_id], dtype=bool)
    return mask, {
        "decisions": len(runs),
        "decisions_held": len(held_ids),
        "rows_held": int(np.count_nonzero(mask)),
        "holdout_frac": HOLDOUT_FRAC,
        "split": "D329 stratified by fingerprint",
    }


def _male(y: FloatArray, y_hat: FloatArray) -> float:
    return float(np.mean(np.abs(y_hat - y))) if y.size else math.nan


def _effective(
    model: LoadedModel | None, X: FloatArray, cal: FloatArray, max_width: float
) -> tuple[FloatArray, dict[str, Any]]:
    """The planner's prediction (model where usable, else calibration) and the
    model's interval facts."""
    if model is None:
        return cal, {}
    out = model.predict(X)
    usable = ~out.ood & ((out.hi - out.lo) <= max_width)
    info = {
        "y_hat": out.y_hat,
        "lo": out.lo,
        "hi": out.hi,
        "usable": usable,
    }
    return np.where(usable, out.y_hat, cal), info


def _families(table: Table, rows: IntArray) -> list[str]:
    return [table.family[i] for i in rows]


def _term(
    table: Table,
    term: str,
    held: IntArray,
    candidate: TrainResult,
    cal_predict: FloatArray,
    reference: Reference,
    settings: CompareSettings,
    gate: GateSettings,
    shipped_learned: bool,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """One term's numbers and the rules it fails (``(rule, detail)``)."""
    member = candidate.member(term)
    y = table.y[held]
    X = table.X[held]
    new_model = (
        load_model(member.artefact, expected_version=member.version)
        if member is not None and member.learned and member.artefact is not None
        else None
    )
    new_eff, new_info = _effective(new_model, X, cal_predict, settings.max_interval_log)
    ref_model = reference.models.get(term)
    ref_eff, _ = _effective(ref_model, X, cal_predict, settings.max_interval_log)
    male_new, male_cal, male_ref = (
        _male(y, new_eff),
        _male(y, cal_predict),
        _male(y, ref_eff),
    )
    families = _families(table, held)
    per_family: dict[str, dict[str, Any]] = {}
    failures: list[tuple[str, str]] = []
    doc: dict[str, Any] = {
        "algorithm": member.algorithm if member is not None else "none",
        "held_rows": len(held),
        "male_new": male_new,
        "male_calibration": male_cal,
        "male_reference": male_ref,
        "reference": "model" if ref_model is not None else "calibration",
    }
    if term in reference.unreadable:
        doc["reference_unreadable"] = reference.unreadable[term]
    if shipped_learned and new_model is None:
        failures.append(
            (
                "too_few_held_out",
                f"{term}: the shipped configuration could not be refitted on the "
                "other rows",
            )
        )
    elif shipped_learned and len(held) < gate.min_held_rows:
        failures.append(
            (
                "too_few_held_out",
                f"{term}: {len(held)} held-out rows < {gate.min_held_rows}",
            )
        )
    if new_model is not None and new_info:
        inside = np.abs(y - new_info["y_hat"]) <= (new_info["hi"] - new_info["y_hat"])
        coverage = float(np.mean(inside)) if len(held) else math.nan
        doc.update(
            {
                "male_model": _male(y, new_info["y_hat"]),
                "coverage": coverage,
                "usable_rate": float(np.mean(new_info["usable"]))
                if len(held)
                else math.nan,
                "model_version": new_model.version,
            }
        )
        if len(held) and coverage < settings.coverage_reject:
            failures.append(
                (
                    "coverage",
                    f"{term}: held-out coverage {coverage:.2f} < "
                    f"{settings.coverage_reject:.2f}",
                )
            )
        for name in sorted(set(families)):
            mask = np.asarray([f == name for f in families])
            rows = int(np.count_nonzero(mask))
            fam_new = _male(y[mask], new_eff[mask])
            fam_cal = _male(y[mask], cal_predict[mask])
            fam_cov = float(np.mean(inside[mask]))
            per_family[name] = {
                "rows": rows,
                "male_new": fam_new,
                "male_calibration": fam_cal,
                "coverage": fam_cov,
            }
            if (
                rows >= settings.family_coverage_min_rows
                and fam_cov < settings.family_coverage_reject
            ):
                failures.append(
                    (
                        "coverage",
                        f"{term}: coverage {fam_cov:.2f} on {name} < "
                        f"{settings.family_coverage_reject:.2f}",
                    )
                )
            if (
                rows >= gate.family_min_rows
                and fam_new - fam_cal > settings.family_tolerance_log + _EPS
            ):
                failures.append(
                    (
                        "family_tolerance",
                        f"{term} on {name}: log error {fam_new:.3f} vs calibration "
                        f"{fam_cal:.3f} (> +{settings.family_tolerance_log})",
                    )
                )
    doc["families"] = per_family
    if len(held) and male_new > male_ref * (1.0 + gate.tolerance) + _EPS:
        failures.append(
            (
                "worse_than_reference",
                f"{term}: held-out log error {male_new:.3f} > {reference.kind} "
                f"{male_ref:.3f} x {1.0 + gate.tolerance:.2f}",
            )
        )
    doc["failed"] = sorted({rule for rule, _ in failures})
    return doc, failures


def run_gate(
    table: Table,
    settings: CompareSettings,
    shipped: TrainResult,
    reference: Reference,
    gate: GateSettings | None = None,
) -> GateResult:
    """Judge ``shipped`` (module docstring)."""
    gate = gate or GateSettings()
    held_mask, split = holdout_mask(table)
    fixed = {m.term: m.config for m in shipped.learned if m.config is not None}
    candidate = train_set(table, settings, allowed=~held_mask, fixed=fixed)
    ctx = FitContext(
        table=table,
        calibration=settings.calibration,
        seed=settings.seed,
        fold="gate",
    )
    terms: dict[str, Any] = {}
    failures: list[tuple[str, str]] = []
    better: list[str] = []
    for member in shipped.members:
        rows = table.rows_of(member.term)
        held = rows[held_mask[rows]]
        fit_rows = rows[~held_mask[rows]]
        if len(held) == 0:
            terms[member.term] = {"algorithm": member.algorithm, "held_rows": 0}
            if member.learned:
                failures.append(
                    ("too_few_held_out", f"{member.term}: no held-out rows")
                )
            continue
        cal = fit_one(_CALIBRATION, ctx, fit_rows).predict(table.X[held], held)
        doc, failed = _term(
            table,
            member.term,
            held,
            candidate,
            cal,
            reference,
            settings,
            gate,
            member.learned,
        )
        terms[member.term] = doc
        failures.extend(failed)
        if (
            member.learned
            and doc["male_calibration"] - doc["male_new"] > settings.min_effect_log
        ):
            better.append(member.term)
    if not better:
        failures.append(
            (
                "not_better",
                "no learned term beats the calibration by more than "
                f"{settings.min_effect_log} log error on the held-out slice",
            )
        )
    order = {rule: i for i, rule in enumerate(GATE_RULES)}
    failures.sort(key=lambda f: order[f[0]])
    rule = failures[0][0] if failures else None
    reason = None
    if failures:
        same = [detail for r, detail in failures if r == rule]
        reason = f"{rule}: " + "; ".join(same)
    doc = clean(
        {
            "accepted": not failures,
            "rule": rule,
            "rules_failed": sorted({r for r, _ in failures}, key=order.__getitem__),
            "reference": {
                "kind": reference.kind,
                "set_version": reference.set_version,
                "unreadable_terms": dict(reference.unreadable),
            },
            "holdout": split,
            "terms": terms,
            "better_terms": better,
            "settings": {
                "tolerance": gate.tolerance,
                "min_held_rows": gate.min_held_rows,
                "family_min_rows": gate.family_min_rows,
                "family_tolerance_log": settings.family_tolerance_log,
                "coverage_reject": settings.coverage_reject,
                "family_coverage_reject": settings.family_coverage_reject,
                "family_coverage_min_rows": settings.family_coverage_min_rows,
                "max_interval_log": settings.max_interval_log,
                "min_effect_log": settings.min_effect_log,
            },
            "candidate_set_version": candidate.set_version,
            "regret": REGRET_PENDING,
            "judged_at_rows": table.n,
        }
    )
    return GateResult(not failures, rule, reason, doc)
