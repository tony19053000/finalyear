"""Calibration: per-term correction factors learned from what really happened
(V0.14 G2, D293, D301 via D306; ADR-0020).

The estimator predicts each term of a candidate's completion time
(:mod:`.estimator`, :mod:`.split`). Calibration multiplies a term's seconds by
a **factor** fitted from past (decision, outcome) pairs: the median of
``actual / predicted`` for that term on that key, shrunk toward 1 and
clamped. Everything here is pure (no database, no clock, no logging); the
controller reads the rows (``controller/calibration_store.py``) and runs the
refresh (``controller/planner_calibration_task.py``).

**Term -> phase mapping** (audited against the agent's phase timings,
``attempt_phase``, ARCHITECTURE §17, and ``compute explain``'s own
``_ACTUAL_PHASE``). A term is calibrated only where one measured phase is
honestly the same work:

=================  =============  ===================================================
term               phase          why
=================  =============  ===================================================
``transfer``       ``stage``      fetching the job's missing trees (whole job), the
                                  lane's trees minus the items (one lane, D264)
``env``            ``env``        building / finding the environment
``compute``        ``run``        the entrypoint's process (whole job only)
``output``         ``collect``    uploading outputs and final logs (whole job only)
``queue``          -              always 0 s ``measured`` (a feasible node has a free
                                  slot); never scaled
``items_fetch``    -              ``no_phase``: per-chunk item fetches happen inside
                                  the lane's ``run`` phase, mixed with compute
``chunk_overhead`` -              ``no_phase``: same (and it is already measured from
                                  chunk history when it exists, D265)
``tail``           -              ``no_phase``: a property of the set, not a phase
``combine``        -              ``no_phase``: a separate attempt, not this one
=================  =============  ===================================================

A split job's **lane** ``run`` phase covers compute + item fetches + chunk
overheads of every chunk it pulled, so ``compute`` of a split candidate is
**not** calibrated (``no_phase``) in this version; its lanes' ``stage`` and
``env`` are (:data:`SPLIT_TERMS`), node by node, before the Mode 3 makespan is
computed - the same physical work as a whole job's ``stage`` / ``env``.

**Matching** (:func:`match`): a ``dispatched`` decision whose attempt ended
``done`` on the node the decision chose (forced / pinned runs count: they
measured something real), with the chosen candidate's stored terms. A whole
job's attempt has role ``run``; a split job counts only when the decision chose
**one lane** ("don't split", role ``lane``) - a Mode 3 set's stored terms are
blends of several lanes and no single phase measures them. Per term: a
``skipped`` phase or a missing one drops the term; a ``measured``-source term
or one predicted under :data:`MIN_PREDICTED_S` is kept for the totals but not
fitted (D293). The prediction is the **uncalibrated** one
(``detail.calibration.raw_s`` when the decision was already calibrated), so a
factor never compounds.

**Fit** (:func:`fit`): per term, per key with the fallback chain
**node + family -> node -> global** (family = the workload family of the
fingerprint when a run record named it, :attr:`Calibration.families`): the
last ``window_runs`` observations of that key (by ``finished_at``), ``m`` =
their median ratio, factor = ``1 + (m - 1) x n / (n + k)`` clamped to
``clamp``. Deterministic: same runs -> same canonical JSON bytes.

**Apply** (:func:`calibrate_estimate`, :func:`calibrate_lane`): a
non-``measured`` term's seconds x its factor, still capped at
:data:`~.estimator.MAX_TERM_S`; its ``source`` is unchanged and its
``detail`` gains ``calibration: {factor, n, key_level, version, raw_s}``. No
calibration -> nothing is touched (bit-identical decisions).

**Safety** (D306): the most recent :data:`HOLDOUT_FRAC` of matched runs are
held out of the fit; a new version activates only if its mean
``|ln(pred_total / actual_total)|`` on them is at most the active version's x
(1 + :data:`GATE_TOLERANCE`) (:func:`gate`). Fewer than :data:`MIN_RUNS`
matched runs, or a fit that produced no factor at all, creates **nothing**
(:func:`refit` returns None): an all-1 version would only add noise rows.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.controller.planner.estimator import bounded
from meshforge.controller.planner.split import Lane
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Estimate,
    EstimateTerm,
    PlanRequest,
    TermSource,
)

__all__ = [
    "CALIBRATED_TERMS",
    "CALIBRATION_FORMAT",
    "GATE_TOLERANCE",
    "HOLDOUT_FRAC",
    "MIN_PREDICTED_S",
    "MIN_RUNS",
    "NOT_CALIBRATED",
    "SPLIT_TERMS",
    "TERM_PHASE",
    "Calibration",
    "CalibrationConfig",
    "CalibrationError",
    "DecisionOutcome",
    "Factor",
    "GateResult",
    "MatchedRun",
    "Refit",
    "TermFactors",
    "TermObservation",
    "calibrate_estimate",
    "calibrate_lane",
    "calibration_from_json",
    "calibration_json",
    "canonical",
    "fit",
    "gate",
    "holdout_error",
    "match",
    "project_calibration",
    "refit",
    "split_holdout",
]

CALIBRATION_FORMAT: Final = 1
"""Version of :func:`calibration_json`'s layout (``factors`` column, replay
``calibration`` block)."""
CALIBRATED_TERMS: Final = ("transfer", "env", "compute", "output")
"""Terms of a whole job that have an honest phase (module docstring)."""
SPLIT_TERMS: Final = ("transfer", "env")
"""Terms of a split job's lane that are calibrated (its stage and env)."""
TERM_PHASE: Final[Mapping[str, str]] = {
    "transfer": "stage",
    "env": "env",
    "compute": "run",
    "output": "collect",
}
NOT_CALIBRATED: Final[Mapping[str, str]] = {
    "queue": "measured",
    "items_fetch": "no_phase",
    "chunk_overhead": "no_phase",
    "tail": "no_phase",
    "combine": "no_phase",
}
"""Terms that are never scaled, and why (shown by ``compute planner
calibration``)."""
MIN_PREDICTED_S: Final = 0.05
"""A term predicted shorter than this is not fitted (D293): its ratio is noise."""
HOLDOUT_FRAC: Final = 0.2
"""D306: the most recent fifth of matched runs judge a new version."""
MIN_RUNS: Final = 2
"""Fewer matched runs than this: no version is created (one to fit, one to
judge, at least)."""
GATE_TOLERANCE: Final = 0.05
"""D306: a new version may be at most 5 % worse than the active one on the
held-out runs."""
_ROUND: Final = 6

type KeyLevel = str
"""``node_family`` | ``node`` | ``global``."""


class CalibrationError(ValueError):
    """A stored calibration (row or replay block) that cannot be read."""


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    """``[controller.planner] calibration_*`` (D302)."""

    window_runs: int = 200
    shrink_k: float = 5.0
    clamp: tuple[float, float] = (0.2, 5.0)


@dataclass(frozen=True, slots=True)
class Factor:
    """One fitted factor: what multiplies the term, from how many runs."""

    factor: float
    n: int
    median: float
    """The raw median of ``actual / predicted`` before shrinkage and clamp."""


@dataclass(frozen=True, slots=True)
class TermFactors:
    """One term's factors at every key level."""

    global_: Factor | None = None
    node: Mapping[str, Factor] = field(default_factory=dict)
    node_family: Mapping[str, Mapping[str, Factor]] = field(default_factory=dict)
    """node_id -> family -> factor."""

    def lookup(
        self, node_id: str | None, family: str | None
    ) -> tuple[Factor, KeyLevel] | None:
        """The most specific factor for ``(node_id, family)``: node + family,
        then node, then global; None = nothing fitted."""
        if node_id is not None:
            if family is not None:
                found = self.node_family.get(node_id, {}).get(family)
                if found is not None:
                    return found, "node_family"
            mine = self.node.get(node_id)
            if mine is not None:
                return mine, "node"
        if self.global_ is not None:
            return self.global_, "global"
        return None


@dataclass(frozen=True, slots=True)
class Calibration:
    """A versioned set of factors (``planner_calibration`` row, D293)."""

    version: int
    """The row's ``version``; 0 = fitted, not stored yet."""
    terms: Mapping[str, TermFactors] = field(default_factory=dict)
    families: Mapping[str, str] = field(default_factory=dict)
    """Workload fingerprint -> workload family (from run records)."""

    def family_of(self, fingerprint: str | None) -> str | None:
        return None if fingerprint is None else self.families.get(fingerprint)

    def lookup(
        self, term: str, node_id: str | None, family: str | None
    ) -> tuple[Factor, KeyLevel] | None:
        table = self.terms.get(term)
        return None if table is None else table.lookup(node_id, family)

    @property
    def factor_count(self) -> int:
        return sum(
            (1 if t.global_ is not None else 0)
            + len(t.node)
            + sum(len(f) for f in t.node_family.values())
            for t in self.terms.values()
        )


# ------------------------------------------------------------------ apply


def _scale(
    term: EstimateTerm, found: tuple[Factor, KeyLevel] | None, version: int
) -> EstimateTerm:
    if found is None or term.source is TermSource.MEASURED:
        return term
    factor, level = found
    return EstimateTerm(
        bounded(term.seconds * factor.factor),
        term.source,
        {
            **term.detail,
            "calibration": {
                "factor": factor.factor,
                "n": factor.n,
                "key_level": level,
                "version": version,
                "raw_s": round(term.seconds, _ROUND),
            },
        },
    )


def calibrate_estimate(
    estimate: Estimate,
    calibration: Calibration,
    *,
    node_id: str,
    family: str | None,
) -> Estimate:
    """A whole job's estimate on ``node_id`` with :data:`CALIBRATED_TERMS`
    scaled (module docstring)."""
    version = calibration.version

    def scaled(name: str, term: EstimateTerm) -> EstimateTerm:
        return _scale(term, calibration.lookup(name, node_id, family), version)

    return dataclasses.replace(
        estimate,
        transfer=scaled("transfer", estimate.transfer),
        env=scaled("env", estimate.env),
        compute=scaled("compute", estimate.compute),
        output=scaled("output", estimate.output),
    )


def calibrate_lane(lane: Lane, calibration: Calibration, *, family: str | None) -> Lane:
    """A split job's lane with its ``stage`` (``transfer``) and ``env`` scaled
    by its node's factors, before any set is costed (:data:`SPLIT_TERMS`)."""
    node_id = lane.node_id
    version = calibration.version
    return dataclasses.replace(
        lane,
        stage=_scale(
            lane.stage, calibration.lookup("transfer", node_id, family), version
        ),
        env=_scale(lane.env, calibration.lookup("env", node_id, family), version),
    )


def project_calibration(
    calibration: Calibration, request: PlanRequest, snapshot: ClusterSnapshot
) -> Calibration:
    """``calibration`` cut to what :func:`~.plan.plan` can look up for
    ``request`` on ``snapshot`` (the replay's ``calibration`` block): every
    global factor, the snapshot's nodes' factors and their node + family
    factors of the request's family only."""
    nodes = {node.node_id for node in snapshot.nodes}
    family = calibration.family_of(request.fingerprint)
    terms = {
        name: TermFactors(
            global_=table.global_,
            node={k: v for k, v in table.node.items() if k in nodes},
            node_family={
                node_id: {family: by_family[family]}
                for node_id, by_family in table.node_family.items()
                if node_id in nodes and family is not None and family in by_family
            },
        )
        for name, table in calibration.terms.items()
    }
    families = (
        {}
        if family is None or request.fingerprint is None
        else {request.fingerprint: family}
    )
    return Calibration(version=calibration.version, terms=terms, families=families)


# ------------------------------------------------------------------ JSON


def _factor_json(factor: Factor) -> dict[str, Any]:
    return {"factor": factor.factor, "n": factor.n, "median": factor.median}


def calibration_json(calibration: Calibration) -> dict[str, Any]:
    """The factors as JSON (the ``factors`` column and the replay block);
    the version is the row's key, not part of it."""
    return {
        "format": CALIBRATION_FORMAT,
        "terms": {
            name: {
                "global": None
                if table.global_ is None
                else _factor_json(table.global_),
                "node": {k: _factor_json(v) for k, v in sorted(table.node.items())},
                "node_family": {
                    node_id: {f: _factor_json(v) for f, v in sorted(by.items())}
                    for node_id, by in sorted(table.node_family.items())
                },
            }
            for name, table in sorted(calibration.terms.items())
        },
        "families": dict(sorted(calibration.families.items())),
    }


def canonical(data: Mapping[str, Any]) -> str:
    """Canonical text (sorted keys, no whitespace, no NaN)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _real(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CalibrationError("a factor is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise CalibrationError("a factor is not finite")
    return number


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CalibrationError("a factor's n is not a count")
    return value


def _factor_from(data: Any) -> Factor:
    if not isinstance(data, Mapping):
        raise CalibrationError("a factor is not an object")
    factor = _real(data["factor"])
    if factor <= 0.0:
        raise CalibrationError("a factor is not positive")
    return Factor(factor=factor, n=_count(data["n"]), median=_real(data["median"]))


def calibration_from_json(version: int, data: Any) -> Calibration:
    """:func:`calibration_json` back; :class:`CalibrationError` when it cannot
    be read (never a partly read calibration)."""
    try:
        if not isinstance(data, Mapping) or data.get("format") != CALIBRATION_FORMAT:
            raise CalibrationError("unsupported calibration format")
        terms: dict[str, TermFactors] = {}
        for name, table in dict(data["terms"]).items():
            raw_global = table["global"]
            terms[str(name)] = TermFactors(
                global_=None if raw_global is None else _factor_from(raw_global),
                node={str(k): _factor_from(v) for k, v in dict(table["node"]).items()},
                node_family={
                    str(node_id): {str(f): _factor_from(v) for f, v in dict(by).items()}
                    for node_id, by in dict(table["node_family"]).items()
                },
            )
        families = {str(k): str(v) for k, v in dict(data["families"]).items()}
    except CalibrationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CalibrationError(
            f"malformed calibration ({type(exc).__name__})"
        ) from None
    return Calibration(version=int(version), terms=terms, families=families)


# ------------------------------------------------------------------ matching


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """One stored ``dispatched`` decision joined to its attempt (the store's
    read; plain values only)."""

    decision_id: int
    attempt_id: str
    chosen_node_id: str | None
    attempt_node_id: str
    attempt_state: str
    attempt_role: str
    candidates: Sequence[Mapping[str, Any]]
    phases_ms: Mapping[str, int | None]
    """phase -> duration ms; None = the phase was ``skipped``."""
    finished_at: dt.datetime
    family: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class TermObservation:
    term: str
    predicted_s: float
    """Uncalibrated prediction."""
    actual_s: float
    fitted: bool
    """False = kept for the totals only (``measured`` or under
    :data:`MIN_PREDICTED_S`)."""
    scaled: bool = True
    """The planner scales this term (its source is not ``measured``)."""


@dataclass(frozen=True, slots=True)
class MatchedRun:
    decision_id: int
    attempt_id: str
    node_id: str
    finished_at: dt.datetime
    terms: tuple[TermObservation, ...]
    family: str | None = None
    fingerprint: str | None = None
    split: bool = False
    """A one-lane split decision (only :data:`SPLIT_TERMS` observed)."""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0.0 else None


def _chosen_row(outcome: DecisionOutcome) -> Mapping[str, Any] | None:
    for row in outcome.candidates:
        if (
            isinstance(row, Mapping)
            and row.get("rank") == 1
            and row.get("node_id") == outcome.chosen_node_id
        ):
            return row
    return None


def _predicted(term: Mapping[str, Any]) -> float | None:
    detail = term.get("detail")
    if isinstance(detail, Mapping):
        calibrated = detail.get("calibration")
        if isinstance(calibrated, Mapping) and "raw_s" in calibrated:
            return _number(calibrated["raw_s"])
    return _number(term.get("seconds"))


def match(outcome: DecisionOutcome) -> MatchedRun | None:
    """The run's term observations, or None when it does not count (module
    docstring: not done, another node, a Mode 3 set, nothing comparable)."""
    if outcome.attempt_state != "done" or outcome.chosen_node_id is None:
        return None
    if outcome.attempt_node_id != outcome.chosen_node_id:
        return None
    row = _chosen_row(outcome)
    if row is None or row.get("mode") == "split":
        return None
    split = bool(row.get("node_set"))
    if outcome.attempt_role != ("lane" if split else "run"):
        return None
    estimate = row.get("estimate")
    terms = estimate.get("terms") if isinstance(estimate, Mapping) else None
    if not isinstance(terms, Mapping):
        return None
    observed: list[TermObservation] = []
    for name in SPLIT_TERMS if split else CALIBRATED_TERMS:
        term = terms.get(name)
        if not isinstance(term, Mapping):
            continue
        actual_ms = outcome.phases_ms.get(TERM_PHASE[name])
        if actual_ms is None or isinstance(actual_ms, bool) or actual_ms < 0:
            continue  # skipped or never reported: nothing honest to compare
        predicted = _predicted(term)
        if predicted is None:
            continue
        scaled = term.get("source") != TermSource.MEASURED.value
        observed.append(
            TermObservation(
                name,
                predicted,
                actual_ms / 1000.0,
                fitted=scaled and predicted >= MIN_PREDICTED_S,
                scaled=scaled,
            )
        )
    if not observed:
        return None
    return MatchedRun(
        decision_id=outcome.decision_id,
        attempt_id=outcome.attempt_id,
        node_id=outcome.attempt_node_id,
        finished_at=outcome.finished_at,
        terms=tuple(observed),
        family=outcome.family,
        fingerprint=outcome.fingerprint,
        split=split,
    )


# ------------------------------------------------------------------ fit


def _order(runs: Iterable[MatchedRun]) -> list[MatchedRun]:
    return sorted(runs, key=lambda r: (r.finished_at, r.decision_id, r.attempt_id))


def split_holdout(
    runs: Iterable[MatchedRun],
) -> tuple[list[MatchedRun], list[MatchedRun]]:
    """``(fit, held_out)``: the most recent :data:`HOLDOUT_FRAC` (at least
    one) are held out, by ``finished_at``; fewer than :data:`MIN_RUNS` runs ->
    everything is ``fit`` and nothing is held."""
    ordered = _order(runs)
    if len(ordered) < MIN_RUNS:
        return ordered, []
    held = max(1, int(len(ordered) * HOLDOUT_FRAC))
    return ordered[:-held], ordered[-held:]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _fit_factor(ratios: Sequence[float], config: CalibrationConfig) -> Factor:
    window = ratios[-max(1, config.window_runs) :]
    n = len(window)
    m = _median(window)
    k = config.shrink_k
    shrunk = 1.0 + (m - 1.0) * n / (n + k)
    low, high = config.clamp
    return Factor(
        factor=round(min(max(shrunk, low), high), _ROUND),
        n=n,
        median=round(m, _ROUND),
    )


def _families(runs: Sequence[MatchedRun]) -> dict[str, str]:
    counts: dict[str, dict[str, int]] = {}
    for run in runs:
        if run.fingerprint is None or run.family is None:
            continue
        by = counts.setdefault(run.fingerprint, {})
        by[run.family] = by.get(run.family, 0) + 1
    return {
        fp: min(by.items(), key=lambda item: (-item[1], item[0]))[0]
        for fp, by in sorted(counts.items())
    }


def fit(runs: Iterable[MatchedRun], config: CalibrationConfig) -> Calibration:
    """Factors from ``runs`` (module docstring); version 0 (not stored)."""
    ordered = _order(runs)
    ratios: dict[str, dict[tuple[str, ...], list[float]]] = {}
    for run in ordered:
        for obs in run.terms:
            if not obs.fitted:
                continue
            ratio = obs.actual_s / obs.predicted_s
            if not math.isfinite(ratio):
                continue
            keys = ratios.setdefault(obs.term, {})
            keys.setdefault(("global",), []).append(ratio)
            keys.setdefault(("node", run.node_id), []).append(ratio)
            if run.family is not None:
                keys.setdefault(("node_family", run.node_id, run.family), []).append(
                    ratio
                )
    terms: dict[str, TermFactors] = {}
    for name, keys in sorted(ratios.items()):
        node: dict[str, Factor] = {}
        node_family: dict[str, dict[str, Factor]] = {}
        global_: Factor | None = None
        for key, values in sorted(keys.items()):
            factor = _fit_factor(values, config)
            if key[0] == "global":
                global_ = factor
            elif key[0] == "node":
                node[key[1]] = factor
            else:
                node_family.setdefault(key[1], {})[key[2]] = factor
        terms[name] = TermFactors(global_=global_, node=node, node_family=node_family)
    return Calibration(version=0, terms=terms, families=_families(ordered))


def holdout_error(
    runs: Iterable[MatchedRun], calibration: Calibration | None
) -> float | None:
    """Mean ``|ln(pred_total / actual_total)|`` of ``runs`` with
    ``calibration`` applied (None = uncalibrated); None when no run has both
    totals positive."""
    errors: list[float] = []
    for run in runs:
        family = None if calibration is None else calibration.family_of(run.fingerprint)
        predicted = 0.0
        actual = 0.0
        for obs in run.terms:
            factor = 1.0
            if calibration is not None and obs.term in TERM_PHASE:
                found = calibration.lookup(obs.term, run.node_id, family)
                if found is not None and obs.scaled:
                    factor = found[0].factor
            predicted += obs.predicted_s * factor
            actual += obs.actual_s
        if predicted > 0.0 and actual > 0.0:
            errors.append(abs(math.log(predicted / actual)))
    if not errors:
        return None
    return math.fsum(errors) / len(errors)


@dataclass(frozen=True, slots=True)
class GateResult:
    accepted: bool
    reason: str | None
    """Why a version was rejected (None = accepted)."""


def gate(new_error: float | None, active_error: float | None) -> GateResult:
    """D306: accept iff ``new <= active x (1 + GATE_TOLERANCE)``; an
    unmeasurable held-out slice rejects (a fit nobody could judge never goes
    live)."""
    if new_error is None or active_error is None:
        return GateResult(False, "holdout_unmeasurable")
    limit = active_error * (1.0 + GATE_TOLERANCE)
    if new_error <= limit:
        return GateResult(True, None)
    return GateResult(
        False,
        f"holdout_error {new_error:.6f} > {1.0 + GATE_TOLERANCE:.2f} x active "
        f"{active_error:.6f}",
    )


@dataclass(frozen=True, slots=True)
class Refit:
    """What one refresh produced (the store writes it)."""

    calibration: Calibration
    fit_inputs: Mapping[str, Any]
    holdout_error_new: float | None
    holdout_error_active: float | None
    gate: GateResult


def refit(
    runs: Iterable[MatchedRun],
    config: CalibrationConfig,
    active: Calibration | None,
) -> Refit | None:
    """Fit on all but the held-out runs, judge both versions on them (D306).
    None = too few runs or nothing to fit (module docstring)."""
    fit_runs, held = split_holdout(runs)
    if not held:
        return None
    calibration = fit(fit_runs, config)
    if calibration.factor_count == 0:
        return None
    new_error = holdout_error(held, calibration)
    active_error = holdout_error(held, active)
    observations = sum(1 for run in fit_runs for obs in run.terms if obs.fitted)
    return Refit(
        calibration=calibration,
        fit_inputs={
            "pairs": len(fit_runs),
            "held_out": len(held),
            "observations": observations,
            "window": config.window_runs,
            "k": config.shrink_k,
            "clamp": list(config.clamp),
            "holdout_frac": HOLDOUT_FRAC,
            "min_predicted_s": MIN_PREDICTED_S,
            "active_version": None if active is None else active.version,
        },
        holdout_error_new=None if new_error is None else round(new_error, 9),
        holdout_error_active=None if active_error is None else round(active_error, 9),
        gate=gate(new_error, active_error),
    )
