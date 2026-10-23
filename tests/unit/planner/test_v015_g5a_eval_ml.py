"""V0.15 G5a (D369/D370): the evaluator's ML rows and the ML criteria.

* ``cost_model:ml`` rows re-plan each record with the family's fold model on
  top of its calibration (D336 precedence); a family without a fold model
  plans with calibration only and counts ``no_fold_model``;
* **criterion verdict table** on synthetic regrets: ``ml_criterion_1`` PASS /
  FAIL / not_separable / too_few_groups, both ``leaked`` on a leaked report,
  ``ml_unavailable`` without ML (never FAIL); ``ml_criterion_2`` per family
  with an absolute percentage-point tolerance, small families listed
  ``too_few_groups`` and never counted as passes;
* mutation guards: criterion 1 must be **paired** (an unpaired mean flips
  the verdict below), the tolerance is in **percentage points** (a ratio
  flips both families below), a small family is never a pass;
* ``eval_version`` 2: the report validates against the API model field for
  field, a version 1 report (no ``ml``) still validates and renders.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from mf_eval_world import fabric, group, records
from mf_ml_stub import Rule, stub_ml

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.common.manifest import OBJECTIVES
from meshforge.controller.api.planner_eval_models import EvalReportOut
from meshforge.controller.planner_eval import core
from meshforge.controller.planner_eval.core import evaluate, ml_fallback
from meshforge.controller.planner_eval.model import (
    COST_MODEL,
    EVAL_VERSION,
    ML_CRITERION_POLICY,
    ML_EVALUATED,
    ML_UNAVAILABLE,
    EvalConfig,
    EvalMl,
    PolicySpec,
    default_policies,
)
from meshforge.controller.planner_eval.report import render_markdown, report_text

pytestmark = pytest.mark.unit

CONFIG = EvalConfig(bootstrap_resamples=500)
# history: node_b looks fastest; measured: node_c is the best
HISTORY = {"node_a": 100.0, "node_b": 10.0, "node_c": 50.0, "node_d": 80.0}
MEASURED = {"node_a": 300.0, "node_b": 200.0, "node_c": 100.0, "node_d": 250.0}
# the ML stub says node_b's compute is 10x the analytical value -> node_c wins
GOOD_RULES = {
    ("compute", "node_b"): Rule(y_hat=math.log(10.0), half_width=0.1),
    ("compute", None): Rule(y_hat=0.0, half_width=0.1),
}


def _world(n: int, *, family: str = "cpu.matmul", campaign: str = "camp_1") -> Any:
    request, snapshot = fabric(HISTORY)
    groups = [
        group(
            f"{family.replace('.', '_')}{i:02d}",
            MEASURED,
            family=family,
            campaign_id=campaign,
        )
        for i in range(n)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    return groups, recs


def _ml(family_inputs: dict[str, Any], **kw: Any) -> EvalMl:
    return EvalMl(ML_EVALUATED, inputs=family_inputs, **kw)


def _verdicts(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in report["ml"]["verdicts"]}


# ------------------------------------------------------------ the ML rows


def test_policy_spec_ml_rows_are_calibrated_cost_model_rows() -> None:
    assert ML_CRITERION_POLICY.key == "cost_model:ml:min_time"
    with pytest.raises(ValueError, match="calibrated cost_model"):
        PolicySpec(COST_MODEL, calibrated=False, ml=True)
    with pytest.raises(ValueError, match="calibrated cost_model"):
        PolicySpec("fastest_node", ml=True)
    keys = [p.key for p in default_policies(ml=True)]
    assert [k for k in keys if ":ml:" in k] == [
        f"cost_model:ml:{objective}" for objective in OBJECTIVES
    ]
    assert keys[-1] == "oracle"
    assert not any(":ml:" in p.key for p in default_policies())


def test_ml_rows_use_the_fold_model_and_pass_criterion_1() -> None:
    groups, recs = _world(12)
    good, stub = stub_ml(GOOD_RULES)
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=_ml({"cpu.matmul": good}),
    )
    rows = {p["key"]: p for p in report["policies"]}
    assert rows["cost_model:calibrated:min_time"]["mean_regret_pct"] == 100.0
    assert rows["cost_model:ml:min_time"]["mean_regret_pct"] == 0.0
    assert stub.calls > 0  # the fold model was really asked
    verdicts = _verdicts(report)
    first = verdicts["ml_criterion_1"]
    assert first["status"] == "PASS" and first["common_groups"] == 12
    assert first["ci_high"] < 0 and first["mean_diff_pct"] == -100.0
    second = verdicts["ml_criterion_2"]
    assert second["status"] == "PASS"
    assert [f["status"] for f in second["families"]] == ["pass"]
    fallback = report["ml"]["fallback"]["total"]
    assert fallback["decisions"] == 12 * 4  # 4 records per group, 12 groups
    assert fallback["no_fold_model"] == 0 and fallback["predictor_error"] == 0
    assert fallback["terms"]["used"] > 0 and fallback["used_rate"] is not None
    assert report["eval_version"] == EVAL_VERSION == 2


def test_a_family_without_fold_model_plans_with_calibration_only() -> None:
    groups, recs = _world(12)
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=_ml({"cpu.matmul": None}),
    )
    rows = {p["key"]: p for p in report["policies"]}
    # ML row == calibrated row exactly (no model: precedence falls through)
    assert (
        rows["cost_model:ml:min_time"]["mean_regret_pct"]
        == rows["cost_model:calibrated:min_time"]["mean_regret_pct"]
    )
    fallback = report["ml"]["fallback"]
    assert fallback["total"]["no_fold_model"] == fallback["total"]["decisions"] == 48
    assert fallback["families"][0]["family"] == "cpu.matmul"
    assert _verdicts(report)["ml_criterion_1"]["status"] == "not_separable"


def test_predictor_errors_and_reasons_are_counted() -> None:
    groups, recs = _world(3)
    broken, _stub = stub_ml(GOOD_RULES, raises=RuntimeError("boom"))
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=_ml({"cpu.matmul": broken}),
    )
    total = report["ml"]["fallback"]["total"]
    assert total["predictor_error"] == total["decisions"] == 12
    assert set(total["terms"]) == {"predictor_error"}
    wide, _ = stub_ml({("compute", None): Rule(y_hat=0.0, half_width=5.0)})
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=_ml({"cpu.matmul": wide}),
    )
    assert "interval_too_wide" in report["ml"]["fallback"]["total"]["terms"]


def test_ml_fallback_of_a_decision_without_ml_is_no_fold_model() -> None:
    from meshforge.controller.planner.plan import plan

    request, snapshot = fabric(HISTORY)
    decision = plan(request, snapshot, "cost_model")
    counts = ml_fallback(decision)
    assert counts == {"decisions": 1, "no_fold_model": 1}


def test_without_ml_the_rows_are_absent_and_verdicts_ml_unavailable() -> None:
    groups, recs = _world(12)
    plain = evaluate(
        groups, recs, {}, default_policies(), CONFIG, campaign_ids=["camp_1"]
    )
    for ml in (None, EvalMl(ML_UNAVAILABLE, "[controller.planner] ml = off")):
        report = evaluate(
            groups,
            recs,
            {},
            default_policies(ml=True),
            CONFIG,
            campaign_ids=["camp_1"],
            ml=ml,
        )
        assert not any(":ml:" in p["key"] for p in report["policies"])
        assert {v["status"] for v in report["ml"]["verdicts"]} == {ML_UNAVAILABLE}
        # the V0.14 part of the report is unchanged by the ML block
        assert report["verdicts"] == plain["verdicts"]
        assert report["policies"] == plain["policies"]
    leaked = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        leaked=True,
    )
    # nothing was claimed, so an unavailable ML is never stamped leaked
    assert {v["status"] for v in leaked["ml"]["verdicts"]} == {ML_UNAVAILABLE}


def test_a_leaked_report_marks_both_ml_verdicts_leaked() -> None:
    groups, recs = _world(12)
    good, _ = stub_ml(GOOD_RULES)
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        leaked=True,
        ml=_ml({"cpu.matmul": good}),
    )
    verdicts = _verdicts(report)
    assert {v["status"] for v in verdicts.values()} == {"leaked"}
    assert verdicts["ml_criterion_1"]["would_be"] == "PASS"
    assert verdicts["ml_criterion_2"]["would_be"] == "PASS"


# ------------------------------------------------------ the verdict table


def _keys(n: int, prefix: str = "g") -> list[tuple[str, str]]:
    return [("camp_1", f"{prefix}{i:02d}") for i in range(n)]


@pytest.mark.parametrize(
    ("diffs", "expected"),
    [
        ([-5.0 - 0.1 * i for i in range(12)], "PASS"),
        ([5.0 + 0.1 * i for i in range(12)], "FAIL"),
        ([(-1.0) ** i * 3.0 for i in range(12)], "not_separable"),
        ([-5.0] * 9, "too_few_groups"),
    ],
)
def test_criterion_1_verdicts(diffs: list[float], expected: str) -> None:
    keys = _keys(len(diffs))
    cal = dict.fromkeys(keys, 50.0)
    ml = {k: 50.0 + d for k, d in zip(keys, diffs, strict=True)}
    out = core._ml_criterion_1(ml, cal, CONFIG, leaked=False)
    assert out["status"] == expected
    assert out["common_groups"] == len(diffs)
    leaked = core._ml_criterion_1(ml, cal, CONFIG, leaked=True)
    assert leaked["status"] == "leaked" and leaked["would_be"] == expected


def test_criterion_1_is_paired_on_common_groups() -> None:
    """Mutation guard (unpaired): ML is worse by 1 point on every common
    group; calibration also has 10 groups ML never counted, all at 100 %.
    Unpaired means would call ML better; paired it is FAIL."""
    common = _keys(12)
    ml = dict.fromkeys(common, 11.0)
    cal = dict.fromkeys(common, 10.0)
    cal.update(dict.fromkeys(_keys(10, prefix="x"), 100.0))
    out = core._ml_criterion_1(ml, cal, CONFIG, leaked=False)
    assert out["status"] == "FAIL"
    assert out["common_groups"] == 12 and out["mean_diff_pct"] == 1.0


def test_criterion_2_tolerance_is_percentage_points_not_a_ratio() -> None:
    """Mutation guard (ratio): famA 10 % -> 14 % passes at +5 points (a
    1.05 ratio would fail it); famB 200 % -> 206 % fails (a ratio would
    pass it)."""
    a, b = _keys(3, "a"), _keys(3, "b")
    family_of = {**dict.fromkeys(a, "famA"), **dict.fromkeys(b, "famB")}
    cal = {**dict.fromkeys(a, 10.0), **dict.fromkeys(b, 200.0)}
    ml = {**dict.fromkeys(a, 14.0), **dict.fromkeys(b, 206.0)}
    out = core._ml_criterion_2(ml, cal, family_of, CONFIG, leaked=False)
    states = {f["family"]: f["status"] for f in out["families"]}
    assert states == {"famA": "pass", "famB": "fail"}
    assert out["status"] == "FAIL" and out["threshold"] == 5.0
    boundary = core._ml_criterion_2(
        {**ml, **dict.fromkeys(b, 205.0)}, cal, family_of, CONFIG, leaked=False
    )
    assert boundary["status"] == "PASS"  # exactly calibrated + 5 points passes


def test_criterion_2_small_families_are_listed_never_passes() -> None:
    big, small = _keys(4, "a"), _keys(2, "b")
    family_of = {**dict.fromkeys(big, "famA"), **dict.fromkeys(small, "famB")}
    cal = {**dict.fromkeys(big, 10.0), **dict.fromkeys(small, 10.0)}
    ml = {**dict.fromkeys(big, 9.0), **dict.fromkeys(small, 90.0)}
    out = core._ml_criterion_2(ml, cal, family_of, CONFIG, leaked=False)
    states = {f["family"]: f["status"] for f in out["families"]}
    assert states == {"famA": "pass", "famB": "too_few_groups"}
    assert out["status"] == "PASS" and "famB" in out["detail"]
    only_small = core._ml_criterion_2(
        dict.fromkeys(small, 1.0),
        dict.fromkeys(small, 10.0),
        dict.fromkeys(small, "famB"),
        CONFIG,
        leaked=False,
    )
    assert only_small["status"] == "too_few_groups"  # never PASS


def test_criterion_2_on_two_families_through_evaluate() -> None:
    groups_a, recs_a = _world(4, family="cpu.matmul")
    groups_b, recs_b = _world(2, family="cpu.sort")
    good, _ = stub_ml(GOOD_RULES)
    report = evaluate(
        groups_a + groups_b,
        recs_a + recs_b,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=_ml({"cpu.matmul": good, "cpu.sort": None}),
    )
    second = _verdicts(report)["ml_criterion_2"]
    states = {f["family"]: f["status"] for f in second["families"]}
    assert states == {"cpu.matmul": "pass", "cpu.sort": "too_few_groups"}
    families = {f["family"]: f for f in report["ml"]["fallback"]["families"]}
    assert families["cpu.sort"]["no_fold_model"] == families["cpu.sort"]["decisions"]
    assert families["cpu.matmul"]["no_fold_model"] == 0


# ------------------------------------------------------------ the report


FOLD = {
    "family": "cpu.matmul",
    "source": "fold",
    "set_version": "sha256:" + "ab" * 32,
    "reason": None,
    "train_rows": 120,
    "trained_families": ["cpu.sort"],
    "excluded_rows": 30,
    "learned_terms": ["compute"],
    "until": "2026-10-23T10:00:00+00:00",
}
TERMS = {
    "compute": {
        "rows": 30,
        "families": 1,
        "male_model": 0.1,
        "male_raw": 0.4,
        "mape_model": 0.11,
        "mape_raw": 0.5,
        "median_ape_model": 0.09,
        "coverage": 0.9,
        "ood_rate": 0.0,
        "usable_rate": 1.0,
    }
}


def test_the_v2_report_validates_against_the_api_model_field_for_field() -> None:
    groups, recs = _world(12)
    good, _ = stub_ml(GOOD_RULES)
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=_ml({"cpu.matmul": good}, folds=[FOLD], terms=TERMS),
    )
    model = EvalReportOut.model_validate(report)
    assert model.eval_version == 2 and model.ml is not None
    assert json.loads(model.model_dump_json()) == json.loads(report_text(report))
    text = render_markdown(report)
    assert "## ML planner" in text and "ml_criterion_1" in text
    assert "### Fold models" in text and FOLD["set_version"] in text
    assert "### Fold models' held-out prediction error per term" in text
    unavailable = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        CONFIG,
        campaign_ids=["camp_1"],
        ml=EvalMl(ML_UNAVAILABLE, "the ml extra is not installed"),
    )
    assert json.loads(
        EvalReportOut.model_validate(unavailable).model_dump_json()
    ) == json.loads(report_text(unavailable))


def test_a_version_1_report_still_validates_and_renders() -> None:
    groups, recs = _world(3)
    report = evaluate(
        groups, recs, {}, default_policies(), CONFIG, campaign_ids=["camp_1"]
    )
    old = {k: v for k, v in report.items() if k != "ml"}
    old["eval_version"] = 1
    model = EvalReportOut.model_validate(old)
    assert model.ml is None
    text = render_markdown(old)
    assert "## ML planner" not in text and "# Planner evaluation report" in text


def test_the_report_with_ml_is_byte_identical_twice() -> None:
    groups, recs = _world(12)

    def once() -> str:
        good, _ = stub_ml(GOOD_RULES)
        return report_text(
            evaluate(
                groups,
                recs,
                {},
                default_policies(ml=True),
                CONFIG,
                campaign_ids=["camp_1"],
                ml=_ml({"cpu.matmul": good}, folds=[FOLD], terms=TERMS),
            )
        )

    assert once() == once()
