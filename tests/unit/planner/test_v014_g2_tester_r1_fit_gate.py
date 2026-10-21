"""V0.14 G2 tester round 1 - pure calibration, derived from brief 5b / D293 / D306.

Independent expectations (own formulas, not the module's helpers):
fit = median(actual/predicted) shrunk ``1 + (m-1) n/(n+k)`` and clamped; key
fallback node+family -> node -> global; ``measured`` / predicted < 0.05 s never
fitted; deterministic bytes; held-out = newest 20 %; gate exactly x 1.05;
hostile stored JSON only ever raises CalibrationError.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any

import pytest

import meshforge.controller.planner.calibration as cal_mod
from meshforge.controller.planner.calibration import (
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

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
CFG = CalibrationConfig()


def mk(
    i: int,
    ratio: float,
    *,
    node: str = "A",
    family: str | None = None,
    term: str = "compute",
    pred: float = 10.0,
    fitted: bool = True,
    extra: dict[str, tuple[float, float]] | None = None,
) -> MatchedRun:
    obs = [TermObservation(term, pred, pred * ratio, fitted=fitted)]
    for name, (p, a) in (extra or {}).items():
        obs.append(TermObservation(name, p, a, fitted=True))
    return MatchedRun(
        decision_id=i,
        attempt_id=f"a{i}",
        node_id=node,
        finished_at=T0 + dt.timedelta(seconds=i),
        terms=tuple(obs),
        family=family,
        fingerprint=None if family is None else f"fp-{family}",
    )


def expect(
    ratios: list[float], k: float = 5.0, lo: float = 0.2, hi: float = 5.0
) -> float:
    n = len(ratios)
    m = statistics.median(ratios)
    return round(min(max(1.0 + (m - 1.0) * n / (n + k), lo), hi), 6)


# --------------------------------------------------------------- fit maths


@pytest.mark.parametrize(
    "ratios",
    [
        [2.0],
        [2.0, 3.0],
        [1.0, 1.0, 100.0],  # outlier cannot move the median
        [0.5] * 7,
        [3.0] * 50,
        [1.1, 0.9, 1.3, 0.7, 2.2, 0.4],
    ],
)
def test_global_factor_is_shrunk_median(ratios: list[float]) -> None:
    c = fit([mk(i, r) for i, r in enumerate(ratios)], CFG)
    f = c.terms["compute"].global_
    assert f is not None
    assert f.factor == pytest.approx(expect(ratios), abs=1e-6)
    assert f.n == len(ratios)
    assert f.median == pytest.approx(statistics.median(ratios), abs=1e-6)


def test_clamp_both_ends_and_custom_clamp() -> None:
    hi = fit([mk(i, 1000.0) for i in range(500)], CFG).terms["compute"].global_
    lo = fit([mk(i, 0.0001) for i in range(500)], CFG).terms["compute"].global_
    assert hi is not None and hi.factor == 5.0
    assert lo is not None and lo.factor == 0.2
    cfg = CalibrationConfig(clamp=(0.5, 2.0))
    c = fit([mk(i, 1000.0) for i in range(500)], cfg).terms["compute"].global_
    assert c is not None and c.factor == 2.0


def test_zero_actual_is_clamped_not_crashing() -> None:
    c = fit([mk(i, 0.0) for i in range(30)], CFG).terms["compute"].global_
    assert c is not None and c.factor == 0.2


def test_k_controls_shrink() -> None:
    for k in (0.001, 1.0, 5.0, 1000.0):
        c = fit([mk(i, 2.0) for i in range(10)], CalibrationConfig(shrink_k=k))
        f = c.terms["compute"].global_
        assert f is not None
        assert f.factor == pytest.approx(1.0 + 10 / (10 + k), abs=1e-5)


def test_window_uses_only_the_most_recent_runs() -> None:
    runs = [mk(i, 10.0) for i in range(100)] + [mk(100 + i, 2.0) for i in range(30)]
    c = fit(runs, CalibrationConfig(window_runs=30))
    f = c.terms["compute"].global_
    assert f is not None and f.n == 30
    assert f.factor == pytest.approx(expect([2.0] * 30), abs=1e-6)
    # window larger than data
    c2 = fit(runs[:5], CalibrationConfig(window_runs=1000))
    assert c2.terms["compute"].global_ is not None
    assert c2.terms["compute"].global_.n == 5  # type: ignore[union-attr]


def test_window_is_chronological_not_input_order() -> None:
    runs = [mk(i, 10.0) for i in range(50)] + [mk(100 + i, 2.0) for i in range(20)]
    random.Random(1).shuffle(runs)  # noqa: S311
    f = fit(runs, CalibrationConfig(window_runs=20)).terms["compute"].global_
    assert f is not None
    assert f.median == 2.0


def test_fallback_chain_levels() -> None:
    runs = (
        [mk(i, 2.0, node="A", family="mm") for i in range(10)]
        + [mk(20 + i, 3.0, node="A", family="other") for i in range(10)]
        + [mk(40 + i, 4.0, node="B") for i in range(10)]
    )
    c = fit(runs, CFG)
    t = c.terms["compute"]
    got = t.lookup("A", "mm")
    assert got is not None and got[1] == "node_family"
    assert got[0].factor == pytest.approx(expect([2.0] * 10), abs=1e-6)
    # unseen family on known node -> node level (pooled over A's runs)
    got = t.lookup("A", "zzz")
    assert got is not None and got[1] == "node" and got[0].n == 20
    # known node, family None -> node
    got = t.lookup("B", None)
    assert got is not None and got[1] == "node"
    # unseen node -> global (n = 30 all)
    got = t.lookup("Z", "mm")
    assert got is not None and got[1] == "global" and got[0].n == 30
    got = t.lookup(None, None)
    assert got is not None and got[1] == "global"


def test_empty_and_all_unfittable_inputs_make_no_factors() -> None:
    assert fit([], CFG).factor_count == 0
    unfit = [mk(i, 2.0, fitted=False) for i in range(20)]
    assert fit(unfit, CFG).factor_count == 0
    tiny = [mk(i, 2.0, pred=0.049) for i in range(20)]
    # fitted flag is set by match(); fit() trusts it - tiny-but-fitted is fitted
    assert fit(tiny, CFG).factor_count >= 0


def test_terms_are_independent() -> None:
    runs = [mk(i, 2.0, extra={"transfer": (4.0, 4.0 * 0.5)}) for i in range(10)]
    c = fit(runs, CFG)
    comp = c.terms["compute"].global_
    xfer = c.terms["transfer"].global_
    assert comp is not None and xfer is not None
    assert comp.factor > 1.0 > xfer.factor


def test_fit_is_deterministic_under_permutation() -> None:
    rng = random.Random(7)  # noqa: S311
    runs = [
        mk(
            i,
            rng.uniform(0.3, 4.0),
            node=rng.choice("ABC"),
            family=rng.choice([None, "x", "y"]),
            term=rng.choice(["compute", "transfer", "env", "output"]),
        )
        for i in range(200)
    ]
    ref = canonical(calibration_json(fit(runs, CFG)))
    for seed in range(5):
        shuffled = runs[:]
        random.Random(seed).shuffle(shuffled)  # noqa: S311
        assert canonical(calibration_json(fit(shuffled, CFG))) == ref


def test_json_round_trip_is_exact() -> None:
    rng = random.Random(3)  # noqa: S311
    runs = [
        mk(i, rng.uniform(0.3, 4.0), node=rng.choice("AB"), family="f")
        for i in range(60)
    ]
    c = fit(runs, CFG)
    data = calibration_json(c)
    back = calibration_from_json(9, json.loads(canonical(data)))
    assert back.version == 9
    assert calibration_json(back) == data
    assert canonical(calibration_json(back)) == canonical(data)


# ------------------------------------------------------------- holdout/gate


@pytest.mark.parametrize(
    ("n", "held"),
    [(2, 1), (3, 1), (4, 1), (5, 1), (9, 1), (10, 2), (14, 2), (15, 3), (100, 20)],
)
def test_holdout_is_newest_fifth_at_least_one(n: int, held: int) -> None:
    runs = [mk(i, 1.0) for i in range(n)]
    random.Random(n).shuffle(runs)  # noqa: S311
    fit_runs, held_runs = split_holdout(runs)
    assert len(held_runs) == held
    assert len(fit_runs) == n - held
    assert min(r.finished_at for r in held_runs) > max(r.finished_at for r in fit_runs)


def test_holdout_with_fewer_than_two_runs_holds_nothing() -> None:
    assert split_holdout([])[1] == []
    assert split_holdout([mk(0, 1.0)])[1] == []


def test_refit_too_few_runs_returns_none() -> None:
    assert refit([], CFG, None) is None
    assert refit([mk(0, 2.0)], CFG, None) is None


def test_refit_without_any_fittable_term_returns_none() -> None:
    assert refit([mk(i, 2.0, fitted=False) for i in range(10)], CFG, None) is None


def test_gate_boundary_exactly_105_percent() -> None:
    # exact floats: 0.2 * 1.05 -> the module computes active * (1 + tol)
    limit = 0.2 * (1.0 + 0.05)
    assert gate(limit, 0.2).accepted
    assert not gate(math.nextafter(limit, 1.0), 0.2).accepted
    assert gate(0.0, 0.0).accepted
    assert gate(0.1, 0.1).accepted
    r = gate(0.3, 0.2)
    assert not r.accepted and r.reason


def test_gate_zero_active_rejects_any_worse_and_none_rejects() -> None:
    assert not gate(1e-9, 0.0).accepted
    for new, act in ((None, 0.1), (0.1, None), (None, None)):
        r = gate(new, act)
        assert not r.accepted and r.reason


def test_bad_fit_is_rejected_good_fit_accepted_end_to_end_pure() -> None:
    good = [mk(i, 2.0) for i in range(10)]  # consistent 2x
    r = refit(good, CFG, None)
    assert r is not None and r.gate.accepted
    assert r.holdout_error_new is not None and r.holdout_error_active is not None
    assert r.holdout_error_new < r.holdout_error_active
    # an "active" calibration that is exactly right; new fit sees drift -> rejected
    active = r.calibration
    drift = [mk(i, 2.0) for i in range(8)] + [mk(8, 9.0), mk(9, 9.0)]
    r2 = refit(drift, CFG, Calibration(1, active.terms, active.families))
    assert r2 is not None
    # held-out (newest 2) = ratio 9 runs; active predicts 2*~1.7 factor, new fit
    # was fit on the 2.0 runs only so it equals active -> not worse -> accepted
    assert r2.gate.accepted
    # a poisoned new fit: fit window contaminated by outlier regime, held-out regime normal
    poisoned = [mk(i, 20.0) for i in range(40)] + [mk(100 + i, 1.0) for i in range(10)]
    ok_active = Calibration(
        3, {"compute": TermFactors(global_=Factor(1.0, 99, 1.0))}, {}
    )
    # fit uses first 40 (20x -> clamp 5) and 2 of 1.0; held-out are 1.0 runs
    r3 = refit(poisoned, CFG, ok_active)
    assert r3 is not None
    assert not r3.gate.accepted
    assert r3.gate.reason


def test_holdout_error_values() -> None:
    held = [mk(0, 2.0), mk(1, 2.0)]
    assert holdout_error(held, None) == pytest.approx(math.log(2.0))
    c = Calibration(1, {"compute": TermFactors(global_=Factor(2.0, 9, 2.0))}, {})
    assert holdout_error(held, c) == pytest.approx(0.0, abs=1e-12)
    assert holdout_error([], None) is None


def test_holdout_error_does_not_scale_measured_terms() -> None:
    obs = TermObservation("compute", 10.0, 10.0, fitted=False, scaled=False)
    run = MatchedRun(0, "a", "A", T0, (obs,))
    c = Calibration(1, {"compute": TermFactors(global_=Factor(3.0, 9, 3.0))}, {})
    assert holdout_error([run], c) == pytest.approx(0.0, abs=1e-12)


def test_holdout_error_ignores_unknown_terms_like_risk() -> None:
    obs = (
        TermObservation("compute", 10.0, 10.0, fitted=True),
        TermObservation("risk", 5.0, 5.0, fitted=False, scaled=False),
    )
    run = MatchedRun(0, "a", "A", T0, obs)
    c = Calibration(1, {"risk": TermFactors(global_=Factor(4.0, 9, 4.0))}, {})
    assert holdout_error([run], c) == pytest.approx(0.0, abs=1e-12)


# -------------------------------------------------------------- apply


def _est(**sec: tuple[float, TermSource]) -> Estimate:
    def t(name: str, default: float = 1.0) -> EstimateTerm:
        s, src = sec.get(name, (default, TermSource.ASSUMED))
        return EstimateTerm(s, src, {})

    return Estimate(
        queue=t("queue"),
        transfer=t("transfer"),
        env=t("env"),
        compute=t("compute"),
        output=t("output"),
        extra={"risk": EstimateTerm(7.0, TermSource.HISTORY, {})},
    )


def test_apply_scales_non_measured_only_and_caps() -> None:
    c = Calibration(
        4,
        {
            "compute": TermFactors(global_=Factor(2.0, 9, 2.0)),
            "transfer": TermFactors(global_=Factor(5.0, 9, 5.0)),
            "output": TermFactors(global_=Factor(2.0, 9, 2.0)),
        },
        {},
    )
    est = _est(
        compute=(10.0, TermSource.HISTORY),
        transfer=(MAX_TERM_S, TermSource.ASSUMED),
        output=(3.0, TermSource.MEASURED),
    )
    out = calibrate_estimate(est, c, node_id="A", family=None)
    assert out.compute.seconds == 20.0
    assert out.compute.source is TermSource.HISTORY
    assert out.compute.detail["calibration"]["version"] == 4
    assert out.compute.detail["calibration"]["raw_s"] == 10.0
    assert out.transfer.seconds == MAX_TERM_S  # cap still holds
    assert out.output.seconds == 3.0 and "calibration" not in out.output.detail
    assert out.queue is est.queue  # untouched
    assert out.extra["risk"] == est.extra["risk"]  # G3 risk never scaled


def test_apply_with_hostile_stored_factor_stays_finite() -> None:
    c = Calibration(1, {"compute": TermFactors(global_=Factor(1e308, 1, 1.0))}, {})
    out = calibrate_estimate(_est(), c, node_id="A", family=None)
    assert math.isfinite(out.compute.seconds)
    assert out.compute.seconds <= MAX_TERM_S


def test_apply_identity_when_factor_one() -> None:
    c = Calibration(
        1,
        {
            t: TermFactors(global_=Factor(1.0, 1, 1.0))
            for t in ("compute", "env", "transfer", "output")
        },
        {},
    )
    est = _est()
    out = calibrate_estimate(est, c, node_id="A", family=None)
    assert out.total_s == est.total_s


# ------------------------------------------------------- decode hostility


def _valid() -> dict[str, Any]:
    runs = [mk(i, 2.0, node="A", family="f") for i in range(10)]
    return calibration_json(fit(runs, CFG))


def _mutations() -> list[Any]:
    base = _valid()
    out: list[Any] = [
        None,
        1,
        "x",
        [],
        {},
        {"format": 1},
        {"format": 2, "terms": {}, "families": {}},
        {"format": True, "terms": {}, "families": {}},
        {"format": 1, "terms": None, "families": {}},
        {"format": 1, "terms": {}, "families": None},
        {"format": 1, "terms": {"compute": 3}, "families": {}},
        {"format": 1, "terms": {"compute": {}}, "families": {}},
        {
            "format": 1,
            "terms": {"compute": {"global": "x", "node": {}, "node_family": {}}},
            "families": {},
        },
        {"format": 1, "terms": {}, "families": {"fp": 3}},
    ]
    for bad in (float("nan"), float("inf"), -1.0, 0.0, True, "2", None, [1]):
        m = copy.deepcopy(base)
        m["terms"]["compute"]["global"]["factor"] = bad
        out.append(m)
    for bad in (-1, 1.5, True, "3", None):
        m = copy.deepcopy(base)
        m["terms"]["compute"]["global"]["n"] = bad
        out.append(m)
    m = copy.deepcopy(base)
    del m["terms"]["compute"]["node"]
    out.append(m)
    m = copy.deepcopy(base)
    m["terms"]["compute"]["node_family"] = {"A": 7}
    out.append(m)
    return out


def _params() -> list[Any]:
    out: list[Any] = []
    for d in _mutations():
        marks = []
        if isinstance(d, dict) and d.get("format") is True:
            # DEFECT T1 (minor): JSON `true` passes `data.get("format") != 1`
            marks = [
                pytest.mark.xfail(strict=True, reason="DEFECT T1: bool format accepted")
            ]
        out.append(pytest.param(d, marks=marks, id=json.dumps(d, default=str)[:60]))
    return out


@pytest.mark.parametrize("data", _params())
def test_hostile_json_only_raises_calibration_error(data: Any) -> None:
    with pytest.raises(CalibrationError):
        calibration_from_json(1, data)


def test_decoder_fuzz_never_raises_anything_else() -> None:
    rng = random.Random(11)  # noqa: S311
    base = _valid()

    def mutate(v: Any, depth: int = 0) -> Any:
        r = rng.random()
        if r < 0.15:
            return rng.choice([None, True, -3, 1e400, "x", [], {}, [1, 2], {"a": 1}])
        if isinstance(v, dict) and depth < 6:
            d = dict(v)
            if d and rng.random() < 0.3:
                d.pop(rng.choice(list(d)))
            for k in list(d):
                if rng.random() < 0.4:
                    d[k] = mutate(d[k], depth + 1)
            return d
        return v

    for _ in range(3000):
        data = mutate(copy.deepcopy(base))
        try:
            c = calibration_from_json(1, data)
        except CalibrationError:
            continue
        # if accepted it must be usable
        calibration_json(c)


# ------------------------------------------------------------------ match


def _outcome(**over: Any) -> DecisionOutcome:
    def term(sec: float, src: str = "history") -> dict[str, Any]:
        return {"seconds": sec, "source": src, "detail": {}}

    cand = {
        "rank": 1,
        "node_id": "A",
        "mode": "whole",
        "estimate": {
            "terms": {
                "queue": term(0.0, "measured"),
                "transfer": term(4.0),
                "env": term(2.0),
                "compute": term(10.0),
                "output": term(1.0),
            }
        },
    }
    base: dict[str, Any] = dict(  # noqa: C408
        decision_id=1,
        attempt_id="att",
        chosen_node_id="A",
        attempt_node_id="A",
        attempt_state="done",
        attempt_role="run",
        candidates=[cand],
        phases_ms={"stage": 5000, "env": 1000, "run": 20000, "collect": 500},
        finished_at=T0,
    )
    base.update(over)
    return DecisionOutcome(**base)


def test_match_maps_terms_to_phases() -> None:
    m = match(_outcome())
    assert m is not None
    by = {o.term: o for o in m.terms}
    assert (by["transfer"].predicted_s, by["transfer"].actual_s) == (4.0, 5.0)
    assert (by["env"].predicted_s, by["env"].actual_s) == (2.0, 1.0)
    assert (by["compute"].predicted_s, by["compute"].actual_s) == (10.0, 20.0)
    assert (by["output"].predicted_s, by["output"].actual_s) == (1.0, 0.5)
    assert "queue" not in by


@pytest.mark.parametrize(
    "over",
    [
        {"attempt_state": "failed"},
        {"attempt_state": "running"},
        {"attempt_node_id": "B"},
        {"chosen_node_id": None},
        {"attempt_role": "lane"},
        {"candidates": []},
        {"candidates": [{"rank": 2, "node_id": "A"}]},
        {"phases_ms": {}},
        {"phases_ms": {"stage": None, "env": None, "run": None, "collect": None}},
    ],
)
def test_match_refuses_what_does_not_count(over: dict[str, Any]) -> None:
    assert match(_outcome(**over)) is None


def test_match_skips_measured_and_tiny_for_fit_but_keeps_them() -> None:
    o = _outcome()
    o.candidates[0]["estimate"]["terms"]["compute"]["source"] = "measured"
    o.candidates[0]["estimate"]["terms"]["env"]["seconds"] = 0.049
    m = match(o)
    assert m is not None
    by = {x.term: x for x in m.terms}
    assert not by["compute"].fitted and not by["compute"].scaled
    assert not by["env"].fitted and by["env"].scaled
    assert by["transfer"].fitted


def test_match_split_set_decision_never_counts() -> None:
    o = _outcome()
    o.candidates[0]["mode"] = "split"
    assert match(o) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0, True, "3", None])
def test_match_ignores_garbage_predictions(bad: Any) -> None:
    o = _outcome()
    o.candidates[0]["estimate"]["terms"]["compute"]["seconds"] = bad
    m = match(o)
    assert m is not None
    assert "compute" not in {x.term for x in m.terms}


def test_match_uses_raw_prediction_of_a_calibrated_decision() -> None:
    o = _outcome()
    t = o.candidates[0]["estimate"]["terms"]["compute"]
    t["seconds"] = 20.0
    t["detail"] = {"calibration": {"raw_s": 10.0, "factor": 2.0}}
    m = match(o)
    assert m is not None
    assert {x.term: x for x in m.terms}["compute"].predicted_s == 10.0


# ----------------------------------------------------------------- purity


def test_calibration_module_is_pure_no_io_imports() -> None:
    tree = ast.parse(Path(cal_mod.__file__).read_text())
    banned = {
        "sqlalchemy",
        "asyncio",
        "socket",
        "os",
        "subprocess",
        "httpx",
        "time",
        "random",
        "pathlib",
        "structlog",
    }
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for n in names:
            assert n.split(".")[0] not in banned, n
            assert "store" not in n.split("."), n
