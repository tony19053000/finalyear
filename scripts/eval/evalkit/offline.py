"""The offline ablations A1-A4 and RQ2 (prediction error), from the reports
the existing tools already produce (D372/D418 - nothing re-implemented).

Inputs (both optional, both as their tools write them):

* the **planner evaluator report** - ``GET /v1/planner/eval-reports/{id}``
  (``compute planner eval --json``) over the L4 data campaign: policies x
  groups, regret vs the oracle, its criterion verdicts and its ML block;
* the **ML comparison report** - ``report.json`` of ``compute planner ml
  compare --ablations`` (``python -m meshforge.controller.ml.train compare``)
  over the same campaign's dataset: per term, model vs calibration MALE with
  its D357 verdict, and the feature-group ablations.

Taken from them:

* **A1 / A2 / A3** = the ML report's ablation rows for the ``hardware``,
  ``load`` and ``cache`` groups, per term (its own paired family bootstrap
  and verdict: ``matters`` / ``not_needed`` / ``not_separable``);
* **A4** (calibration off) = per evaluator group, the regret of
  ``cost_model:uncalibrated:min_time`` minus ``cost_model:calibrated:
  min_time`` (percentage points) over the groups counted for both, with the
  seeded bootstrap CI of its mean - ``matters`` (CI above 0: calibration
  lowers regret), ``not_needed`` (below 0), else ``not_separable``; fewer
  than ``min_groups`` groups -> ``not_separable`` (``too_few_groups``);
* **RQ2** = the ML report's per-term MALE of the model and of the
  calibration and the ``vs_calibration`` verdict, and the evaluator's policy
  rows and verdicts as printed.

Pure; the same two files give the same block.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from evalkit.canonical import num
from evalkit.schedule import derive_seed

__all__ = ["ABLATION_GROUPS", "offline_block"]

ABLATION_GROUPS: Final = {"A1": "hardware", "A2": "load", "A3": "cache"}
_UNCALIBRATED: Final = "cost_model:uncalibrated:min_time"
_CALIBRATED: Final = "cost_model:calibrated:min_time"


def _f(value: Any) -> float | None:
    return num(float(value)) if isinstance(value, int | float) else None


def _a4(
    report: Mapping[str, Any], *, seed: int, resamples: int, min_groups: int
) -> dict[str, Any]:
    from meshforge.bench.stats import bootstrap_mean_ci

    diffs: dict[str, float] = {}
    for group in report.get("groups") or ():
        picks = {
            p.get("key"): p
            for p in group.get("picks") or ()
            if p.get("status") == "counted" and p.get("regret_pct") is not None
        }
        if _UNCALIBRATED in picks and _CALIBRATED in picks:
            key = f"{group.get('campaign_id')}/{group.get('group_id')}"
            diffs[key] = float(picks[_UNCALIBRATED]["regret_pct"]) - float(
                picks[_CALIBRATED]["regret_pct"]
            )
    values = [diffs[k] for k in sorted(diffs)]
    if not values:
        return {
            "groups": 0,
            "mean_pp": None,
            "ci": None,
            "verdict": "not_separable",
            "reason": "too_few_groups",
        }
    ci = bootstrap_mean_ci(values, seed=seed, resamples=resamples)
    if len(values) < min_groups:
        verdict, reason = "not_separable", "too_few_groups"
    elif ci.low > 0.0:
        verdict, reason = "matters", "ci_above_zero"
    elif ci.high < 0.0:
        verdict, reason = "not_needed", "ci_below_zero"
    else:
        verdict, reason = "not_separable", "ci_contains_zero"
    return {
        "groups": len(values),
        "mean_pp": num(ci.mean),
        "ci": [num(ci.low), num(ci.high)],
        "verdict": verdict,
        "reason": reason,
    }


def _eval_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "eval_version": report.get("eval_version"),
        "campaign_ids": list(report.get("campaign_ids") or ()),
        "leaked": bool(report.get("leaked")),
        "policies": [
            {
                "key": row.get("key"),
                "counted_groups": row.get("counted_groups"),
                "mean_regret_pct": _f(row.get("mean_regret_pct")),
                "ci_low": _f((row.get("ci_pct") or {}).get("low")),
                "ci_high": _f((row.get("ci_pct") or {}).get("high")),
                "within_threshold_pct": _f(row.get("within_threshold_pct")),
            }
            for row in report.get("policies") or ()
        ],
        "verdicts": [
            {
                "criterion": v.get("criterion"),
                "status": v.get("status"),
                "would_be": v.get("would_be"),
            }
            for v in report.get("verdicts") or ()
        ],
    }


def _ml_summary(doc: Mapping[str, Any]) -> dict[str, Any]:
    terms = []
    for term in doc.get("terms") or ():
        pooled = term.get("pooled") or {}
        verdict = term.get("vs_calibration") or {}
        terms.append(
            {
                "term": term.get("term"),
                "rows": term.get("rows"),
                "families": term.get("families"),
                "status": term.get("status"),
                "male_model": _f(pooled.get("male_model")),
                "male_calibration": _f(pooled.get("male_calibration")),
                "male_raw": _f(pooled.get("male_raw")),
                "mape_model": _f(pooled.get("mape_model")),
                "mape_calibration": _f(pooled.get("mape_calibration")),
                "verdict": verdict.get("verdict"),
                "reason": verdict.get("reason"),
                "ci": [_f(verdict.get("low")), _f(verdict.get("high"))],
            }
        )
    ablations: dict[str, Any] = {}
    block = doc.get("ablations") or {}
    for label, group in ABLATION_GROUPS.items():
        rows = [
            {
                "term": term,
                "status": row.get("status"),
                "families": row.get("families"),
                "delta_male_mean": _f(row.get("mean")),
                "ci": [_f(row.get("low")), _f(row.get("high"))],
                "delta_male_pooled": _f(row.get("delta_male_pooled")),
                "verdict": row.get("verdict"),
                "reason": row.get("reason"),
            }
            for term, items in sorted((block.get("terms") or {}).items())
            for row in items or ()
            if row.get("group") == group
        ]
        ablations[label] = {
            "group": group,
            "status": "evaluated" if rows else "not_in_report",
            "terms": rows,
        }
    return {
        "dataset_digest": (doc.get("dataset") or {}).get("digest"),
        "terms": terms,
        "ablations": ablations,
    }


def offline_block(
    eval_report: Mapping[str, Any] | None,
    ml_report: Mapping[str, Any] | None,
    *,
    seed: int,
    resamples: int,
    min_groups: int,
) -> dict[str, Any]:
    """The analysis document's ``offline`` block (module docstring)."""
    out: dict[str, Any] = {"status": "evaluated"}
    if eval_report is None:
        out["evaluator"] = None
        out["A4"] = {"status": "not_given"}
    else:
        out["evaluator"] = _eval_summary(eval_report)
        out["A4"] = {
            "status": "evaluated",
            **_a4(
                eval_report,
                seed=derive_seed(seed, "a4"),
                resamples=resamples,
                min_groups=min_groups,
            ),
        }
    if ml_report is None:
        out["ml_compare"] = None
        for label in ABLATION_GROUPS:
            out[label] = {"status": "not_given"}
    else:
        summary = _ml_summary(ml_report)
        out["ml_compare"] = {
            "dataset_digest": summary["dataset_digest"],
            "terms": summary["terms"],
        }
        out.update(summary["ablations"])
    return out
