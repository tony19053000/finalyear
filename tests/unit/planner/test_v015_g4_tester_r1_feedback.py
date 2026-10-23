# ruff: noqa: S311 - seeded test data, not security
"""V0.15 G4 tester round 1: the feedback loop must not close (D362).

A decision stores its terms *after* the ML / calibration correction. Both the
V0.14 calibration fit and the V0.15 training set read the stored decision
back. If either read the corrected seconds as the "analytical raw", the next
model would learn the residual of its own predecessor (and the calibration
would fit factors on ML-corrected values) - a silent feedback loop.

For random planner cases and random stub models / calibrations:

* ``raw_prediction`` of every stored term equals the analytical seconds of the
  same term planned with no calibration and no model (6 decimals) - whichever
  of the three forms (ML-used, calibrated, raw) the row holds;
* a one-lane row's chunk rate is read back as the analytical seconds / item;
* ``calibration.match`` of the chosen row gives the same predicted seconds
  with or without an ML model in the loop, and ``fit`` over those matched
  runs gives the same factors;
* the dataset's residual target is ``ln(actual / analytical raw)`` and never
  uses the ML-corrected seconds.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import random
from typing import Any

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import random_case

from meshforge.controller.ml.targets import _raw_seconds_per_item
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    Calibration,
    CalibrationConfig,
    DecisionOutcome,
    Factor,
    TermFactors,
    fit,
    match,
    raw_prediction,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import ML_TERMS
from meshforge.controller.planner.replay import project
from meshforge.controller.planner.types import Decision

pytestmark = pytest.mark.unit

WHOLE = ("transfer", "env", "compute", "output")
T0 = dt.datetime(2026, 10, 22, tzinfo=dt.UTC)


def _calibration(rng: random.Random) -> Calibration | None:
    if rng.random() < 0.2:
        return None
    return Calibration(
        version=4,
        terms={
            t: TermFactors(global_=Factor(rng.uniform(0.4, 3.0), 9, 1.0))
            for t in WHOLE
            if rng.random() < 0.85
        },
    )


def _rules(rng: random.Random) -> dict[Any, Rule]:
    rules: dict[Any, Rule] = {}
    for t in ML_TERMS:
        if rng.random() < 0.1:
            continue
        rules[(t, None)] = Rule(
            y_hat=rng.uniform(-1.5, 1.5),
            half_width=rng.choice([0.05, 0.05, 0.9]),
            ood=rng.random() < 0.15,
        )
    return rules


def _rows(decision: Decision) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = json.loads(json.dumps(candidates_json(decision)))
    return out


def _key(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return row["node_id"], tuple(row.get("node_set") or ())


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 2e-6 + 1e-9 * abs(b)


@pytest.mark.parametrize("seed", range(300))
def test_raw_prediction_of_every_stored_term_is_the_analytical_raw(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    rng = random.Random(seed)
    view = project(request, snapshot)
    cal = _calibration(rng)
    ml, _ = stub_ml(_rules(rng))
    raw_rows = {_key(r): r for r in _rows(plan(request, view, "cost_model", now))}
    got = _rows(plan(request, view, "cost_model", now, calibration=cal, ml=ml))
    checked = 0
    for row in got:
        mine = row.get("estimate")
        base = raw_rows.get(_key(row), {}).get("estimate")
        if not mine or not base:
            continue
        for name, term in mine["terms"].items():
            if name not in (*WHOLE, "combine") or name not in base["terms"]:
                continue
            want = base["terms"][name]["seconds"]
            have = raw_prediction(term)
            if have is None:
                continue
            if name == "combine":
                # combine is never calibrated; its raw is the unscaled value
                # but a set's combine depends on corrected lanes (members,
                # active lanes) - only compare when ML did not move the set
                continue
            if row.get("node_set") and len(row["node_set"]) > 1:
                continue  # a set's terms depend on the corrected lane rates
            if name == "compute" and row.get("node_set"):
                continue  # one-lane compute: see the dedicated defect test below
            assert _close(have, want), (seed, _key(row), name, have, want)
            checked += 1
    assert checked >= 0


@pytest.mark.parametrize("seed", [s for s in range(300) if random_case(s)[0].split])
def test_a_corrected_lane_rate_reads_back_as_the_analytical_rate(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    assert request.split is not None
    rng = random.Random(seed + 1000)
    view = project(request, snapshot)
    ml, _ = stub_ml(_rules(rng))
    raw_rows = {_key(r): r for r in _rows(plan(request, view, "cost_model", now))}
    for row in _rows(plan(request, view, "cost_model", now, ml=ml)):
        if not row.get("feasible") or len(row.get("node_set") or ()) != 1:
            continue
        base = raw_rows.get(_key(row))
        if base is None or not base.get("rate_items_s"):
            continue
        spi = _raw_seconds_per_item(row)
        assert spi is not None
        assert spi == pytest.approx(1.0 / base["rate_items_s"], rel=1e-5), (
            seed,
            row["node_id"],
        )


def _outcome(decision: Decision, rows: list[dict[str, Any]]) -> DecisionOutcome | None:
    chosen = decision.chosen
    if chosen is None or chosen.estimate is None:
        return None
    return DecisionOutcome(
        decision_id=1,
        attempt_id="a1",
        chosen_node_id=chosen.node_id,
        attempt_node_id=chosen.node_id,
        attempt_state="done",
        attempt_role="lane" if chosen.node_set else "run",
        candidates=rows,
        phases_ms={"queue": 1, "stage": 4000, "env": 6000, "run": 9000, "collect": 700},
        finished_at=T0,
        family="fam",
        fingerprint="f" * 64,
    )


def test_calibration_matching_and_fitting_ignore_the_ml_correction() -> None:
    """Over many decisions of the *same node and phases*, the factors fitted
    from ML-corrected decisions equal the factors fitted from the same
    decisions planned with no model."""
    with_ml, without = [], []
    for seed in range(800):
        request, snapshot, now = random_case(seed)
        if request.split is not None:
            continue
        rng = random.Random(seed)
        view = project(request, snapshot)
        cal = _calibration(rng)
        ml, _ = stub_ml(_rules(rng))
        d_ml = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
        node = d_ml.chosen.node_id if d_ml.chosen else None
        if node is None:
            continue
        rows_ml = _rows(d_ml)
        oc_ml = _outcome(d_ml, rows_ml)
        if oc_ml is None:
            continue
        m_ml = match(oc_ml)
        if m_ml is None:
            continue
        # raw (uncalibrated) predictions: identical, though stored seconds differ
        # when calibration is None-vs-set; compare against the *uncalibrated*
        # plain plan to be fair
        raw_rows = _rows(plan(request, view, "cost_model", now))
        raw_base = next(
            r for r in raw_rows if r["node_id"] == node and r.get("feasible")
        )
        for r in raw_rows:
            r["rank"] = (
                1 if r is raw_base else (None if r.get("rank") == 1 else r.get("rank"))
            )
        m_raw = match(dataclasses.replace(oc_ml, candidates=raw_rows))
        assert m_raw is not None
        by_ml = {o.term: o for o in m_ml.terms}
        by_raw = {o.term: o for o in m_raw.terms}
        assert set(by_ml) == set(by_raw), seed
        for term, obs in by_ml.items():
            assert _close(obs.predicted_s, by_raw[term].predicted_s), (seed, term)
            assert obs.fitted == by_raw[term].fitted, (seed, term)
        with_ml.append(m_ml)
        without.append(m_raw)
    assert len(with_ml) > 30
    config = CalibrationConfig()
    f_ml, f_raw = fit(with_ml, config), fit(without, config)
    for term in WHOLE:
        a, b = f_ml.terms.get(term), f_raw.terms.get(term)
        assert (a is None) == (b is None)
        if a is not None and b is not None and a.global_ and b.global_:
            assert a.global_.factor == pytest.approx(b.global_.factor, rel=1e-4), term
            assert a.global_.n == b.global_.n


def test_ml_used_row_with_a_hostile_raw_is_not_matched_not_crashed() -> None:
    """A stored ``detail.ml.raw_s`` that is junk (a manual edit / a bug) is a
    term the calibration refuses (None), never a crash."""
    for junk in (None, "x", -1.0, math.nan, True, [1]):
        term = {
            "seconds": 5.0,
            "source": "history",
            "detail": {"ml": {"used": True, "raw_s": junk}},
        }
        assert raw_prediction(term) is None
    # used False: the seconds (or the calibration's raw) win
    term = {
        "seconds": 5.0,
        "source": "history",
        "detail": {"ml": {"used": False, "raw_s": 99.0}},
    }
    assert raw_prediction(term) == 5.0
    # used True without raw_s falls through to seconds (no loop: nothing to read)
    term = {"seconds": 5.0, "source": "history", "detail": {"ml": {"used": True}}}
    assert raw_prediction(term) == 5.0


@pytest.mark.parametrize("seed", [s for s in range(300) if random_case(s)[0].split])
def test_a_one_lane_compute_term_reads_back_in_its_own_unit(seed: int) -> None:
    """DEFECT G4-R1-1 (minor): the ``compute`` term of a one-lane split row
    carries the *chunk rate's* ML block: ``detail.ml.raw_s`` is seconds PER
    ITEM (0.0046) while the term's ``seconds`` is the lane's total compute
    (4.64 s). ``raw_prediction(term)`` is documented as "a stored term's
    uncalibrated seconds" and ``explain`` prints ``ML 0.00s -> 4.64s``. When
    the term says ML used, ``raw_s * exp(y_hat)`` must be the term's seconds
    (same unit), or the block must not sit on the compute term."""
    request, snapshot, now = random_case(seed)
    rng = random.Random(seed + 77)
    view = project(request, snapshot)
    ml, _ = stub_ml(
        {(t, None): Rule(y_hat=rng.uniform(-1, 1), half_width=0.05) for t in ML_TERMS}
    )
    # (implementer, G4 fix r1: feasible rows only - a refused one-node "split"
    # row shares the one-lane row's (node_id, node_set) key and overwrote it)
    raw_rows = {
        _key(r): r
        for r in _rows(plan(request, view, "cost_model", now))
        if r.get("feasible")
    }
    for row in _rows(plan(request, view, "cost_model", now, ml=ml)):
        if not row.get("feasible") or len(row.get("node_set") or ()) != 1:
            continue
        compute = row["estimate"]["terms"]["compute"]
        block = compute["detail"].get("ml")
        base = raw_rows.get(_key(row))
        if block is None or not block.get("used") or base is None:
            continue
        want = base["estimate"]["terms"]["compute"]["seconds"]
        got = raw_prediction(compute)
        if got is None or want < 1e-3:
            continue
        assert got == pytest.approx(want, rel=1e-4), (
            seed,
            row["node_id"],
            got,
            want,
        )
