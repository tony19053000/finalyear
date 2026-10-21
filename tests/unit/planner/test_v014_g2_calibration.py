"""V0.14 G2 (D293, D306): the pure calibration - match, fit, apply, gate.

Hand-built runs pin the maths (median, shrinkage ``1 + (m - 1) n / (n + k)``,
clamp, the node + family -> node -> global fallback, the window, outliers,
determinism); hand-built stored decisions pin what :func:`match` counts (the
term -> phase mapping, skipped phases, ``measured`` and tiny terms, raw
predictions of an already calibrated decision, Mode 3 sets never); the gate is
checked exactly at x 1.05.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from typing import Any

import pytest

from meshforge.controller.planner.calibration import (
    GATE_TOLERANCE,
    MIN_PREDICTED_S,
    Calibration,
    CalibrationConfig,
    CalibrationError,
    DecisionOutcome,
    Factor,
    MatchedRun,
    TermFactors,
    TermObservation,
    calibrate_estimate,
    calibration_from_json,
    calibration_json,
    canonical,
    fit,
    gate,
    holdout_error,
    match,
    refit,
    split_holdout,
)
from meshforge.controller.planner.estimator import MAX_TERM_S
from meshforge.controller.planner.types import Estimate, EstimateTerm, TermSource

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
CFG = CalibrationConfig(window_runs=200, shrink_k=5.0, clamp=(0.2, 5.0))


def _run(
    i: int,
    *,
    node: str = "n1",
    family: str | None = None,
    fingerprint: str | None = None,
    terms: dict[str, tuple[float, float]] | None = None,
    fitted: bool = True,
) -> MatchedRun:
    observed = tuple(
        TermObservation(name, pred, actual, fitted=fitted)
        for name, (pred, actual) in (terms or {"compute": (10.0, 20.0)}).items()
    )
    return MatchedRun(
        decision_id=i,
        attempt_id=f"att_{i}",
        node_id=node,
        finished_at=T0 + dt.timedelta(minutes=i),
        terms=observed,
        family=family,
        fingerprint=fingerprint,
    )


def _shrunk(m: float, n: int, k: float = 5.0) -> float:
    return round(min(max(1.0 + (m - 1.0) * n / (n + k), 0.2), 5.0), 6)


# ---------------------------------------------------------------- fit maths


def test_median_ratio_shrunk_toward_one() -> None:
    ratios = [2.0, 1.0, 3.0, 2.5, 1.5]  # median 2.0
    runs = [_run(i, terms={"compute": (10.0, 10.0 * r)}) for i, r in enumerate(ratios)]
    cal = fit(runs, CFG)
    found = cal.lookup("compute", "n1", None)
    assert found is not None
    factor, level = found
    assert level == "node"
    assert factor.n == 5
    assert factor.median == 2.0
    assert factor.factor == _shrunk(2.0, 5)  # 1 + 1 x 5/10 = 1.5
    assert factor.factor == 1.5
    assert cal.terms["compute"].global_ == factor


def test_even_count_median_is_the_mean_of_the_middle_two() -> None:
    runs = [_run(i, terms={"env": (4.0, 4.0 * r)}) for i, r in enumerate((1.0, 3.0))]
    factor = fit(runs, CFG).lookup("env", "n1", None)
    assert factor is not None and factor[0].median == 2.0


@pytest.mark.parametrize(("ratio", "bound"), [(100.0, 5.0), (0.001, 0.2)])
def test_factors_are_clamped(ratio: float, bound: float) -> None:
    runs = [_run(i, terms={"compute": (1.0, ratio)}) for i in range(500)]
    found = fit(runs, CFG).lookup("compute", "n1", None)
    assert found is not None and found[0].factor == bound


def test_clamp_is_configurable() -> None:
    runs = [_run(i, terms={"compute": (1.0, 9.0)}) for i in range(1000)]
    cfg = CalibrationConfig(window_runs=1000, shrink_k=5.0, clamp=(0.5, 2.0))
    found = fit(runs, cfg).lookup("compute", "n1", None)
    assert found is not None and found[0].factor == 2.0


def test_one_odd_run_barely_moves_the_factor_and_outliers_do_not_drag_the_median() -> (
    None
):
    one = fit([_run(0, terms={"compute": (1.0, 3.0)})], CFG)
    found = one.lookup("compute", "n1", None)
    assert found is not None
    assert found[0].factor == _shrunk(3.0, 1) == round(1.0 + 2.0 / 6.0, 6)
    many = [_run(i, terms={"compute": (10.0, 12.0)}) for i in range(20)]
    many += [_run(100 + i, terms={"compute": (10.0, 1e6)}) for i in range(3)]
    found = fit(many, CFG).lookup("compute", "n1", None)
    assert found is not None and found[0].median == 1.2


def test_window_keeps_the_most_recent_runs_of_the_key() -> None:
    old = [_run(i, terms={"compute": (10.0, 30.0)}) for i in range(50)]
    new = [_run(100 + i, terms={"compute": (10.0, 10.0)}) for i in range(10)]
    cfg = CalibrationConfig(window_runs=10, shrink_k=5.0, clamp=(0.2, 5.0))
    found = fit([*new, *old], cfg).lookup("compute", "n1", None)  # input order free
    assert found is not None
    assert found[0].n == 10 and found[0].median == 1.0 and found[0].factor == 1.0


def test_fallback_chain_node_family_then_node_then_global() -> None:
    runs = [
        *[
            _run(i, node="n1", family="matmul", terms={"compute": (1.0, 3.0)})
            for i in range(5)
        ],
        *[_run(10 + i, node="n1", terms={"compute": (1.0, 1.0)}) for i in range(5)],
        *[_run(20 + i, node="n2", terms={"compute": (1.0, 2.0)}) for i in range(5)],
    ]
    cal = fit(runs, CFG)
    nf = cal.lookup("compute", "n1", "matmul")
    assert nf is not None and nf[1] == "node_family" and nf[0].n == 5
    assert nf[0].median == 3.0
    node = cal.lookup("compute", "n1", "other_family")
    assert node is not None and node[1] == "node" and node[0].n == 10
    assert node[0].median == 2.0  # ratios 3,3,3,3,3,1,1,1,1,1
    glob = cal.lookup("compute", "n9", "matmul")
    assert glob is not None and glob[1] == "global" and glob[0].n == 15
    assert cal.lookup("compute", None, None) == (cal.terms["compute"].global_, "global")
    assert cal.lookup("env", "n1", None) is None  # nothing fitted for env


def test_family_map_names_the_commonest_family_of_a_fingerprint() -> None:
    runs = [
        _run(0, fingerprint="fp", family="b"),
        _run(1, fingerprint="fp", family="a"),
        _run(2, fingerprint="fp", family="b"),
        _run(3, fingerprint="tie", family="z"),
        _run(4, fingerprint="tie", family="y"),
        _run(5, fingerprint="nofam"),
    ]
    cal = fit(runs, CFG)
    assert cal.families == {"fp": "b", "tie": "y"}
    assert cal.family_of("fp") == "b"
    assert cal.family_of(None) is None and cal.family_of("nofam") is None


def test_empty_and_all_unfitted_runs_give_no_factor() -> None:
    assert fit([], CFG).factor_count == 0
    unfitted = [_run(i, fitted=False) for i in range(10)]
    assert fit(unfitted, CFG).factor_count == 0
    assert refit(unfitted, CFG, None) is None


def test_fit_is_deterministic_bytes_whatever_the_input_order() -> None:
    rng = random.Random(7)  # noqa: S311 - seeded test data
    runs = [
        _run(
            i,
            node=rng.choice(["n1", "n2", "n3"]),
            family=rng.choice([None, "f1", "f2"]),
            fingerprint=rng.choice(["a", "b", None]),
            terms={
                t: (rng.uniform(0.1, 50.0), rng.uniform(0.0, 80.0))
                for t in ("transfer", "env", "compute", "output")
            },
        )
        for i in range(300)
    ]
    texts = set()
    for _ in range(5):
        rng.shuffle(runs)
        texts.add(canonical(calibration_json(fit(runs, CFG))))
    assert len(texts) == 1


# ---------------------------------------------------------------- JSON codec


def test_json_round_trip_and_refusals() -> None:
    cal = Calibration(
        version=3,
        terms={
            "compute": TermFactors(
                global_=Factor(1.2, 9, 1.4),
                node={"n1": Factor(0.8, 4, 0.7)},
                node_family={"n1": {"f": Factor(2.0, 2, 3.5)}},
            ),
            "env": TermFactors(),
        },
        families={"fp": "f"},
    )
    data = json.loads(canonical(calibration_json(cal)))
    assert calibration_from_json(3, data) == cal

    def table(glob: Any, node: Any = None) -> dict[str, Any]:
        return {
            "format": 1,
            "terms": {
                "x": {
                    "global": glob,
                    "node": {} if node is None else node,
                    "node_family": {},
                }
            },
            "families": {},
        }

    for bad in (
        None,
        [],
        {"format": 2, "terms": {}, "families": {}},
        {"format": 1, "families": {}},
        table({"factor": "1", "n": 1, "median": 1.0}),
        table({"factor": 0.0, "n": 1, "median": 1.0}),
        table({"factor": 1.0, "n": -1, "median": 1.0}),
        table({"factor": 1.0, "n": True, "median": 1.0}),
        table(None, node=[]),
        {"format": 1, "terms": {}, "families": {"fp": 3}},
    ):
        with pytest.raises(CalibrationError):
            calibration_from_json(1, bad)


def test_an_integral_factor_read_back_as_int_is_a_float() -> None:
    data = calibration_json(
        Calibration(1, {"compute": TermFactors(global_=Factor(5.0, 3, 9.0))})
    )
    data["terms"]["compute"]["global"]["factor"] = 5  # jsonb may hand back 5
    back = calibration_from_json(1, data)
    factor = back.terms["compute"].global_
    assert factor is not None and isinstance(factor.factor, float)


# ---------------------------------------------------------------- apply


def _estimate(**sources: TermSource) -> Estimate:
    def term(name: str, seconds: float) -> EstimateTerm:
        return EstimateTerm(seconds, sources.get(name, TermSource.HISTORY), {"k": 1})

    return Estimate(
        queue=EstimateTerm(0.0, TermSource.MEASURED, {"free_slots": 1}),
        transfer=term("transfer", 4.0),
        env=term("env", 2.0),
        compute=term("compute", 10.0),
        output=term("output", 1.0),
    )


def _cal(**factors: float) -> Calibration:
    return Calibration(
        version=7,
        terms={
            name: TermFactors(global_=Factor(value, 3, value))
            for name, value in factors.items()
        },
    )


def test_apply_scales_each_term_and_records_how() -> None:
    est = calibrate_estimate(
        _estimate(), _cal(compute=1.5, transfer=0.5), node_id="n1", family=None
    )
    assert est.compute.seconds == 15.0
    assert est.transfer.seconds == 2.0
    assert est.env.seconds == 2.0 and "calibration" not in est.env.detail
    assert est.compute.source is TermSource.HISTORY  # source unchanged
    assert est.compute.detail == {
        "k": 1,
        "calibration": {
            "factor": 1.5,
            "n": 3,
            "key_level": "global",
            "version": 7,
            "raw_s": 10.0,
        },
    }
    assert est.total_s == 0.0 + 2.0 + 2.0 + 15.0 + 1.0


def test_measured_terms_are_never_scaled() -> None:
    est = calibrate_estimate(
        _estimate(env=TermSource.MEASURED),
        _cal(env=3.0, queue=3.0),
        node_id="n1",
        family=None,
    )
    assert est.env.seconds == 2.0 and "calibration" not in est.env.detail
    assert est.queue.seconds == 0.0


def test_factor_one_keeps_every_second_and_the_scaled_term_stays_capped() -> None:
    base = _estimate()
    est = calibrate_estimate(
        base,
        _cal(transfer=1.0, env=1.0, compute=1.0, output=1.0),
        node_id="n",
        family=None,
    )
    assert est.total_s == base.total_s
    for name in ("transfer", "env", "compute", "output"):
        assert est.terms()[name].seconds == base.terms()[name].seconds
    huge = Estimate(
        queue=base.queue,
        transfer=base.transfer,
        env=base.env,
        compute=EstimateTerm(MAX_TERM_S, TermSource.ASSUMED),
        output=base.output,
    )
    capped = calibrate_estimate(huge, _cal(compute=5.0), node_id="n", family=None)
    assert capped.compute.seconds == MAX_TERM_S


# ---------------------------------------------------------------- matching


def _row(**terms: Any) -> dict[str, Any]:
    return {
        "node_id": "n1",
        "mode": "offload",
        "rank": 1,
        "estimate": {"total_s": 0.0, "terms": terms},
    }


def _t(seconds: float, source: str = "history", **detail: Any) -> dict[str, Any]:
    return {"seconds": seconds, "source": source, "detail": detail}


def _outcome(row: dict[str, Any], **over: Any) -> DecisionOutcome:
    base: dict[str, Any] = {
        "decision_id": 1,
        "attempt_id": "att_1",
        "chosen_node_id": "n1",
        "attempt_node_id": "n1",
        "attempt_state": "done",
        "attempt_role": "run",
        "candidates": [
            {"node_id": "n2", "mode": "offload", "rank": 2, "estimate": None},
            row,
        ],
        "phases_ms": {
            "queue": 5,
            "stage": 3000,
            "env": 1000,
            "run": 20000,
            "collect": 500,
        },
        "finished_at": T0,
    }
    base.update(over)
    return DecisionOutcome(**base)


def test_match_maps_terms_to_their_phases() -> None:
    row = _row(
        queue=_t(0.0, "measured"),
        transfer=_t(2.0),
        env=_t(1.0, "assumed"),
        compute=_t(10.0),
        output=_t(0.25),
    )
    run = match(_outcome(row, family="f", fingerprint="fp"))
    assert run is not None
    got = {o.term: (o.predicted_s, o.actual_s, o.fitted) for o in run.terms}
    assert got == {
        "transfer": (2.0, 3.0, True),
        "env": (1.0, 1.0, True),
        "compute": (10.0, 20.0, True),
        "output": (0.25, 0.5, True),
    }
    assert run.family == "f" and run.fingerprint == "fp" and not run.split


@pytest.mark.parametrize(
    "over",
    [
        {"attempt_state": "failed"},
        {"attempt_state": "lost"},
        {"attempt_node_id": "n2"},  # ran elsewhere than the decision chose
        {"chosen_node_id": None},
        {"attempt_role": "lane"},  # a whole job's decision, a lane attempt
        {"attempt_role": "combine"},
        {"candidates": []},
    ],
)
def test_match_refuses_runs_that_measured_something_else(over: dict[str, Any]) -> None:
    assert match(_outcome(_row(compute=_t(10.0)), **over)) is None


def test_skipped_and_missing_phases_drop_their_term() -> None:
    row = _row(transfer=_t(2.0), env=_t(5.0), compute=_t(10.0))
    run = match(_outcome(row, phases_ms={"stage": None, "run": 1000}))
    assert run is not None
    assert [o.term for o in run.terms] == ["compute"]
    assert match(_outcome(row, phases_ms={"stage": None})) is None


def test_measured_and_tiny_terms_are_kept_for_totals_but_not_fitted() -> None:
    row = _row(
        transfer=_t(0.0, "measured"),
        env=_t(MIN_PREDICTED_S / 2),
        compute=_t(MIN_PREDICTED_S),
    )
    run = match(_outcome(row))
    assert run is not None
    fitted = {o.term: (o.fitted, o.scaled) for o in run.terms}
    assert fitted == {
        "transfer": (False, False),
        "env": (False, True),
        "compute": (True, True),
    }


def test_an_already_calibrated_prediction_is_read_raw() -> None:
    row = _row(compute=_t(15.0, calibration={"factor": 1.5, "raw_s": 10.0}))
    run = match(_outcome(row))
    assert run is not None and run.terms[0].predicted_s == 10.0


def test_split_sets_never_match_and_one_lane_matches_stage_and_env_only() -> None:
    set_row = {**_row(compute=_t(10.0)), "mode": "split", "node_set": ["n1", "n2"]}
    assert match(_outcome(set_row, attempt_role="lane")) is None
    lane = {
        **_row(transfer=_t(2.0), env=_t(1.0), compute=_t(10.0), output=_t(1.0)),
        "node_set": ["n1"],
    }
    assert match(_outcome(lane, attempt_role="run")) is None
    run = match(_outcome(lane, attempt_role="lane"))
    assert run is not None and run.split
    assert [o.term for o in run.terms] == ["transfer", "env"]


@pytest.mark.parametrize("bad", [None, "x", True, -1.0, float("nan"), float("inf")])
def test_unusable_numbers_drop_the_term(bad: Any) -> None:
    run = match(
        _outcome(_row(compute=_t(10.0), env={"seconds": bad, "source": "history"}))
    )
    assert run is not None and [o.term for o in run.terms] == ["compute"]


# ---------------------------------------------------------------- holdout + gate


def test_holdout_is_the_most_recent_fifth() -> None:
    runs = [_run(i) for i in range(10)]
    fit_runs, held = split_holdout(reversed(runs))
    assert [r.decision_id for r in held] == [8, 9]
    assert [r.decision_id for r in fit_runs] == list(range(8))
    assert split_holdout([_run(0)]) == ([_run(0)], [])
    fit2, held2 = split_holdout([_run(0), _run(1)])
    assert [r.decision_id for r in held2] == [1] and len(fit2) == 1


def test_holdout_error_is_mean_abs_log_ratio_of_totals() -> None:
    import math

    runs = [
        _run(0, terms={"compute": (10.0, 20.0), "transfer": (10.0, 20.0)}),
        _run(1, terms={"compute": (10.0, 10.0)}),
    ]
    assert holdout_error(runs, None) == pytest.approx(math.log(2) / 2)
    cal = _cal(compute=2.0, transfer=2.0)
    assert holdout_error(runs, cal) == pytest.approx(math.log(2) / 2)
    zero = [_run(0, terms={"compute": (10.0, 0.0)})]
    assert holdout_error(zero, None) is None


def test_gate_accepts_exactly_at_the_tolerance_and_rejects_above() -> None:
    active = 0.4
    limit = active * (1.0 + GATE_TOLERANCE)
    assert gate(limit, active).accepted
    assert gate(active, active).accepted
    assert gate(0.0, active).accepted
    above = gate(limit + 1e-12, active)
    assert not above.accepted and above.reason is not None
    assert above.reason.startswith("holdout_error ")
    assert gate(None, 0.1).reason == "holdout_unmeasurable"
    assert gate(0.1, None).reason == "holdout_unmeasurable"


def test_refit_judges_the_new_fit_against_the_active_one_on_held_runs() -> None:
    runs = [_run(i, terms={"compute": (10.0, 20.0)}) for i in range(20)]
    first = refit(runs, CFG, None)
    assert first is not None
    assert first.fit_inputs["pairs"] == 16 and first.fit_inputs["held_out"] == 4
    assert first.gate.accepted  # 2x reality: any factor > 1 beats identity
    assert (
        first.holdout_error_new is not None and first.holdout_error_active is not None
    )
    assert first.holdout_error_new < first.holdout_error_active
    # an active version that is already exact cannot be replaced by a worse one
    exact = _cal(compute=2.0)
    second = refit(runs, CFG, exact)
    assert second is not None and not second.gate.accepted
    assert second.fit_inputs["active_version"] == 7


def test_refit_needs_two_runs() -> None:
    assert refit([], CFG, None) is None
    assert refit([_run(0)], CFG, None) is None
    assert refit([_run(0), _run(1)], CFG, None) is not None
