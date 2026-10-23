"""V0.15 G5a tester: adversarial evaluator ML rows, criteria and regret gate."""

from __future__ import annotations

import copy
import json
import math
import random
from typing import Any

import pytest
from mf_eval_world import fabric, group, records
from mf_ml_stub import Rule, stub_ml

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.controller.ml import regret
from meshforge.controller.planner_eval import core
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.model import (
    ML_EVALUATED,
    EvalConfig,
    EvalMl,
    default_policies,
)
from meshforge.controller.planner_eval.regret_gate import (
    RegretCheck,
    ml_regret_check,
    regret_gate,
)

pytestmark = pytest.mark.unit

CONFIG = EvalConfig(bootstrap_resamples=300)
HISTORY = {"node_a": 100.0, "node_b": 10.0, "node_c": 50.0, "node_d": 80.0}
MEASURED = {"node_a": 300.0, "node_b": 200.0, "node_c": 100.0, "node_d": 250.0}
GOOD = {
    ("compute", "node_b"): Rule(y_hat=math.log(10.0), half_width=0.1),
    ("compute", None): Rule(y_hat=0.0, half_width=0.1),
}
NEUTRAL = {("compute", None): Rule(y_hat=0.0, half_width=0.1)}


def _world(n: int, family: str = "cpu.matmul") -> tuple[list[Any], list[Any]]:
    request, snapshot = fabric(HISTORY)
    groups = [
        group(
            f"{family.replace('.', '_')}{i:02d}",
            MEASURED,
            family=family,
            campaign_id="camp_1",
        )
        for i in range(n)
    ]
    return groups, [r for g in groups for r in records(g, request, snapshot, reps=1)]


def _run(groups: list[Any], recs: list[Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=EvalMl(ML_EVALUATED, inputs=inputs),
    )


def _v(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in report["ml"]["verdicts"]}


# ----------------------------------------------------------- evaluator edges


def test_empty_campaign_with_ml_gives_too_few_groups_not_a_crash() -> None:
    report = _run([], [], {})
    v = _v(report)
    assert v["ml_criterion_1"]["status"] == "too_few_groups"
    assert v["ml_criterion_2"]["status"] == "too_few_groups"
    assert report["ml"]["fallback"]["total"]["decisions"] == 0


def test_a_single_group_is_never_a_pass() -> None:
    groups, recs = _world(1)
    good, _ = stub_ml(GOOD)
    v = _v(_run(groups, recs, {"cpu.matmul": good}))
    assert v["ml_criterion_1"]["status"] == "too_few_groups"
    assert v["ml_criterion_2"]["status"] == "too_few_groups"


def test_ml_inputs_for_unrelated_families_are_ignored() -> None:
    groups, recs = _world(12)
    good, stub = stub_ml(GOOD)
    report = _run(groups, recs, {"some.other": good})
    assert stub.calls == 0
    assert report["ml"]["fallback"]["total"]["no_fold_model"] == 12 * 4
    # no model -> ML row == calibrated row: criterion 1 is not a PASS
    assert _v(report)["ml_criterion_1"]["status"] == "not_separable"


@pytest.mark.parametrize("delta", [1, -1])
def test_a_malformed_predictor_answer_count_degrades_to_calibration(delta: int) -> None:
    groups, recs = _world(12)
    bad, stub = stub_ml(GOOD)
    stub.answer_count_delta = delta
    report = _run(groups, recs, {"cpu.matmul": bad})
    total = report["ml"]["fallback"]["total"]
    assert total["predictor_error"] > 0
    # never a PASS built on garbage
    assert _v(report)["ml_criterion_1"]["status"] != "PASS"


@pytest.mark.parametrize("exc", [RuntimeError("x"), ValueError("y"), OSError("z")])
def test_a_raising_predictor_is_counted_not_propagated(exc: BaseException) -> None:
    groups, recs = _world(12)
    bad, stub = stub_ml(GOOD)
    stub.raises = exc
    report = _run(groups, recs, {"cpu.matmul": bad})
    assert report["ml"]["fallback"]["total"]["predictor_error"] > 0


def test_an_always_ood_model_is_calibration_and_counted() -> None:
    groups, recs = _world(12)
    ood, _ = stub_ml(
        {("compute", None): Rule(y_hat=5.0, half_width=0.1, ood=True, ood_reason="ood")}
    )
    report = _run(groups, recs, {"cpu.matmul": ood})
    terms = report["ml"]["fallback"]["total"]["terms"]
    assert terms.get("used", 0) == 0 and terms.get("ood", 0) > 0
    rows = {p["key"]: p for p in report["policies"]}
    assert (
        rows["cost_model:ml:min_time"]["mean_regret_pct"]
        == rows["cost_model:calibrated:min_time"]["mean_regret_pct"]
    )


def test_a_too_wide_interval_is_not_used() -> None:
    groups, recs = _world(12)
    wide, _ = stub_ml({("compute", None): Rule(y_hat=math.log(10.0), half_width=50.0)})
    report = _run(groups, recs, {"cpu.matmul": wide})
    assert report["ml"]["fallback"]["total"]["terms"].get("used", 0) == 0


def test_an_infinite_or_nan_prediction_never_wins() -> None:
    groups, recs = _world(12)
    for y in (math.inf, -math.inf, math.nan):
        bad, _ = stub_ml({("compute", None): Rule(y_hat=y, half_width=0.1)})
        report = _run(groups, recs, {"cpu.matmul": bad})  # must not raise
        json.dumps(report, allow_nan=False)  # and the report stays valid JSON


def test_record_order_does_not_change_the_report() -> None:
    groups, recs = _world(12)
    good, _ = stub_ml(GOOD)
    a = _run(groups, recs, {"cpu.matmul": good})
    shuffled = recs[:]
    random.Random(7).shuffle(shuffled)  # noqa: S311
    good2, _ = stub_ml(GOOD)
    b = _run(groups[::-1], shuffled, {"cpu.matmul": good2})
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_two_families_one_without_fold_model_still_judged_per_family() -> None:
    g1, r1 = _world(12, "cpu.matmul")
    g2, r2 = _world(12, "cpu.fft")
    good, _ = stub_ml(GOOD)
    report = _run(g1 + g2, r1 + r2, {"cpu.matmul": good, "cpu.fft": None})
    fam = {f["family"]: f for f in _v(report)["ml_criterion_2"]["families"]}
    assert fam["cpu.matmul"]["status"] == "pass"
    assert fam["cpu.fft"]["status"] == "pass"  # equal to calibrated: within +5pp
    by = {f["family"]: f for f in report["ml"]["fallback"]["families"]}
    assert by["cpu.fft"]["no_fold_model"] == 12 * 4
    assert by["cpu.matmul"]["no_fold_model"] == 0


# ---------------------------------------------- criterion boundaries (pure)


def _c2(
    ml: dict[Any, float], cal: dict[Any, float], fam: dict[Any, str], tol: float = 5.0
) -> dict[str, Any]:
    cfg = EvalConfig(ml_family_tolerance_pp=tol)
    return core._ml_criterion_2(ml, cal, fam, cfg, False)


def _keys(n: int) -> list[tuple[str, str]]:
    return [("c", f"g{i}") for i in range(n)]


def test_criterion_2_exact_tolerance_passes_and_epsilon_over_fails() -> None:
    ks = _keys(3)
    fam = dict.fromkeys(ks, "f")
    cal = dict.fromkeys(ks, 10.0)
    assert _c2(dict.fromkeys(ks, 15.0), cal, fam)["status"] == "PASS"
    assert _c2(dict.fromkeys(ks, 15.001), cal, fam)["status"] == "FAIL"


def test_criterion_2_exactly_three_groups_counts_two_does_not() -> None:
    fam3 = dict.fromkeys(_keys(3), "f")
    bad3 = dict.fromkeys(_keys(3), 90.0)
    cal3 = dict.fromkeys(_keys(3), 0.0)
    assert _c2(bad3, cal3, fam3)["status"] == "FAIL"
    fam2 = dict.fromkeys(_keys(2), "f")
    out = _c2(dict.fromkeys(_keys(2), 90.0), dict.fromkeys(_keys(2), 0.0), fam2)
    assert out["status"] == "too_few_groups"
    assert out["families"][0]["status"] == "too_few_groups"


def test_criterion_2_a_failing_big_family_beats_passing_ones_and_small_bad_ones_dont_fail() -> (
    None
):
    ks = _keys(3) + [("c", f"h{i}") for i in range(3)] + [("c", "s0")]
    fam = dict.fromkeys(ks[:3], "ok") | dict.fromkeys(ks[3:6], "bad") | {ks[6]: "small"}
    cal = dict.fromkeys(ks, 0.0)
    ml = dict.fromkeys(ks[:3], 0.0) | dict.fromkeys(ks[3:6], 50.0) | {ks[6]: 99.0}
    assert _c2(ml, cal, fam)["status"] == "FAIL"
    ml["h0" if False else ks[3]] = 0.0
    ml.update(dict.fromkeys(ks[3:6], 0.0))
    out = _c2(ml, cal, fam)
    assert out["status"] == "PASS"
    assert {f["family"]: f["status"] for f in out["families"]}[
        "small"
    ] == "too_few_groups"


def test_criterion_2_only_counts_groups_present_in_both_rows() -> None:
    ks = _keys(5)
    fam = dict.fromkeys(ks, "f")
    out = _c2(dict.fromkeys(ks[:2], 0.0), dict.fromkeys(ks, 0.0), fam)
    assert out["common_groups"] == 2 and out["status"] == "too_few_groups"


def test_criterion_1_all_zero_diff_is_not_separable() -> None:
    ks = _keys(15)
    z = dict.fromkeys(ks, 7.0)
    out = core._ml_criterion_1(z, dict(z), CONFIG, False)
    assert out["status"] == "not_separable" and out["mean_diff_pct"] == 0.0


def test_criterion_1_leaked_keeps_would_be() -> None:
    ks = _keys(15)
    out = core._ml_criterion_1(
        dict.fromkeys(ks, 0.0), dict.fromkeys(ks, 50.0), CONFIG, True
    )
    assert out["status"] == "leaked" and out["would_be"] == "PASS"


# ---------------------------------------------------------- regret gate edges


def test_regret_gate_zero_and_zero_passes_zero_current_positive_candidate_rejected() -> (
    None
):
    assert regret_gate(0.0, 0.0, 12).status == "passed"
    assert regret_gate(0.001, 0.0, 12).status == "rejected"


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_regret_gate_non_finite_is_rejected_not_raised(bad: float) -> None:
    assert regret_gate(bad, 1.0, 12).status == "rejected"
    assert regret_gate(1.0, bad, 12).status == "rejected"


def test_regret_gate_exactly_105_percent_passes_just_over_rejects() -> None:
    assert regret_gate(1.05, 1.0, 12).status == "passed"
    assert regret_gate(1.14 * 1.05, 1.14, 12).status in {"passed", "rejected"}
    assert regret_gate(1.0500001, 1.0, 12).status == "rejected"


def test_ml_regret_check_with_no_groups_is_skipped() -> None:
    good, _ = stub_ml(GOOD)
    check = ml_regret_check([], [], None, good, None)
    assert check.status == "skipped" and check.groups == 0


def test_ml_regret_check_candidate_that_raises_is_not_better_than_calibration() -> None:
    groups, recs = _world(12)
    good, _ = stub_ml(GOOD)
    broken, bstub = stub_ml(GOOD)
    bstub.raises = RuntimeError("boom")
    # broken candidate degrades to calibration (regret 100) vs a good serving set
    check = ml_regret_check(groups, recs, None, broken, good)
    assert check.status == "rejected"


def _doc() -> dict[str, Any]:
    return {
        "set_version": "sha256:" + "cd" * 32,
        "accepted": True,
        "rule": None,
        "reason": None,
        "gate": {
            "accepted": True,
            "rule": None,
            "rules_failed": [],
            "regret": {"status": "not_run"},
        },
    }


def _rej() -> regret.RegretOutcome:
    return regret.RegretOutcome(
        RegretCheck("rejected", 12, 9.0, 1.0, "holdout_regret 9 > 1.05 x 1"), None
    )


def test_apply_does_not_mutate_its_input_and_is_idempotent() -> None:
    doc = _doc()
    before = copy.deepcopy(doc)
    once = regret.apply(doc, _rej())
    assert doc == before
    twice = regret.apply(once, _rej())
    assert twice["gate"]["rules_failed"] == ["regret_worse"]
    assert twice["rule"] == "regret_worse" and twice["accepted"] is False
    assert twice["gate"]["accepted"] is False


def test_apply_without_a_gate_block_and_preserving_earlier_failures() -> None:
    out = regret.apply({"accepted": True}, _rej())
    assert out["gate"]["regret"]["status"] == "rejected" and out["accepted"] is False
    doc = _doc()
    doc["gate"]["rules_failed"] = ["min_rows"]
    out = regret.apply(doc, _rej())
    assert out["gate"]["rules_failed"] == ["min_rows", "regret_worse"]


def test_regret_block_with_calibration_only_and_note() -> None:
    out = regret.regret_block(
        regret.RegretOutcome(RegretCheck("skipped", 0, None, None, "r"), None, "note")
    )
    assert out["current"] == {
        "kind": "calibration",
        "set_version": None,
        "note": "note",
    }
    json.dumps(out, allow_nan=False)
