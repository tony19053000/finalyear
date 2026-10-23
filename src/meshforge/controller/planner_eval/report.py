"""An evaluation report as canonical JSON text and as Markdown (V0.14 G4, D297/D318).

* :func:`report_text` - sorted keys, no whitespace, no NaN: the same report
  gives the same bytes (the determinism test compares them).
* :func:`render_markdown` - the human report: a ``LEAKED`` banner when the
  calibration may have seen the data, the verdicts, the per-policy table
  (mean regret with its CI, share within the threshold, unmeasured and
  no-replay counts), the per-family table of the criterion policy and the
  oracle, the counts and the calibrations used. Every value comes from the
  report; nothing is recomputed, so JSON and Markdown cannot disagree.
  V0.15 G5a (``eval_version`` 2): the ML section - its two criteria (the
  per-family table of criterion 2), the fold models, the fallback counts
  and the fold models' per-term prediction error; a version 1 report has no
  ``ml`` block and renders as before.

Pure (no I/O).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = ["render_markdown", "report_text"]

_DASH: Final = "-"


def report_text(report: Mapping[str, Any]) -> str:
    """Canonical JSON text of ``report``."""
    return json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )


def _num(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DASH
    return f"{value:.{digits}f}"


def _cell(value: Any) -> str:
    """A table cell: no pipe or newline can break the table."""
    text = _DASH if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    # every cell is already escaped by its producer (_cell or a number)
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _ci(row: Mapping[str, Any]) -> str:
    ci = row.get("ci_pct")
    if not isinstance(ci, Mapping) or ci.get("low") is None:
        return _DASH
    return f"[{_num(ci.get('low'))}, {_num(ci.get('high'))}]"


def _ci_label(level: Any) -> str:
    """The CI column header from the report's own level (0.95 -> "95 % CI")."""
    if (
        isinstance(level, (int, float))
        and not isinstance(level, bool)
        and 0 < level < 1
    ):
        return f"{level * 100:g} % CI"
    return "CI"


def _latency_lines(latency: Any) -> list[str]:
    """V0.14 G6 (D324): the decision-latency section (none for a report
    stored before G6)."""
    if not isinstance(latency, Mapping):
        return []
    judged = latency.get("judged")
    judged = judged if isinstance(judged, Mapping) else {}
    live = latency.get("judged_on") == "live_nodes"  # D332; older: every row
    what = "live nodes (online, connected, accepting jobs)" if live else "nodes"
    lines = [
        "",
        "## Decision latency (criterion 3)",
        "",
        f"- dispatched decisions timed: {_cell(latency.get('decisions'))} "
        f"(without a time: {_cell(latency.get('without_latency'))})",
        f"- p50 {_num(latency.get('p50_ms'))} ms, p99 {_num(latency.get('p99_ms'))} "
        f"ms, max {_num(latency.get('max_ms'))} ms (nearest rank)",
        f"- over the {_num(latency.get('budget_ms'), 0)} ms budget: "
        f"{_cell(latency.get('over_budget'))}",
        f"- criterion 3 judges snapshots of >= {_cell(latency.get('node_count'))} "
        f"{what}: {_cell(judged.get('decisions'))} decisions, p99 "
        f"{_num(judged.get('p99_ms'))} ms (at least "
        f"{_cell(latency.get('min_decisions'))} needed)",
        "",
    ]
    buckets = latency.get("by_node_count")
    lines += _table(
        ("snapshot nodes", "decisions", "p99 ms"),
        [
            (_cell(nodes), _cell(b.get("decisions")), _num(b.get("p99_ms")))
            for nodes, b in (buckets.items() if isinstance(buckets, Mapping) else ())
            if isinstance(b, Mapping)
        ],
    )
    live_buckets = latency.get("by_live_node_count")
    if isinstance(live_buckets, Mapping) and live_buckets:  # as the CLI
        lines.append("")
        lines += _table(
            ("live nodes", "decisions", "p99 ms"),
            [
                (_cell(nodes), _cell(b.get("decisions")), _num(b.get("p99_ms")))
                for nodes, b in live_buckets.items()
                if isinstance(b, Mapping)
            ],
        )
    return lines


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _ml_lines(ml: Any) -> list[str]:
    """V0.15 G5a (D369/D370): the ML section (none for a version 1
    report)."""
    if not isinstance(ml, Mapping):
        return []
    settings = _map(ml.get("settings"))
    lines = [
        "",
        "## ML planner (cost_model:ml vs calibrated analytical)",
        "",
        f"- status: {_cell(ml.get('status'))}"
        + (f" ({_cell(ml.get('reason'))})" if ml.get("reason") else ""),
        f"- criterion row `{_cell(settings.get('criterion_policy'))}` vs "
        f"`{_cell(settings.get('compared_with'))}`; family tolerance "
        f"{_num(settings.get('family_tolerance_pp'))} % points, families need >= "
        f"{_cell(settings.get('min_family_groups'))} common groups",
        "",
    ]
    verdicts = [v for v in ml.get("verdicts") or () if isinstance(v, Mapping)]
    lines += _table(
        ("verdict", "status", "would be", "groups", "mean diff %", "CI", "detail"),
        [
            (
                _cell(v.get("name")),
                _cell(v.get("status")),
                _cell(v.get("would_be")),
                _cell(v.get("common_groups")),
                _num(v.get("mean_diff_pct")),
                _DASH
                if v.get("ci_low") is None
                else f"[{_num(v.get('ci_low'))}, {_num(v.get('ci_high'))}]",
                _cell(v.get("detail")),
            )
            for v in verdicts
        ],
    )
    families = [
        f
        for v in verdicts
        if v.get("name") == "ml_criterion_2"
        for f in v.get("families") or ()
        if isinstance(f, Mapping)
    ]
    if families:
        lines += ["", "### ML criterion 2 per family", ""]
        lines += _table(
            (
                "family",
                "groups",
                "ML regret %",
                "calibrated regret %",
                "diff",
                "status",
            ),
            [
                (
                    _cell(f.get("family")),
                    _cell(f.get("common_groups")),
                    _num(f.get("mean_regret_ml_pct")),
                    _num(f.get("mean_regret_calibrated_pct")),
                    _num(f.get("diff_pct")),
                    _cell(f.get("status")),
                )
                for f in families
            ],
        )
    folds = [f for f in ml.get("folds") or () if isinstance(f, Mapping)]
    if folds:
        lines += ["", "### Fold models (one per held-out family, never registered)", ""]
        lines += _table(
            ("family", "set version", "training rows", "model", "why not"),
            [
                (
                    _cell(f.get("family")),
                    _cell(f.get("set_version")),
                    _cell(f.get("train_rows")),
                    _cell(", ".join(f.get("learned_terms") or ()) or None),
                    _cell(f.get("reason")),
                )
                for f in folds
            ],
        )
    fallback = _map(ml.get("fallback"))
    rows = [
        ("all", fallback.get("total")),
        *(
            (f.get("family"), f)
            for f in fallback.get("families") or ()
            if isinstance(f, Mapping)
        ),
    ]
    lines += ["", "### What ML did in the chosen plans", ""]
    lines += _table(
        (
            "family",
            "decisions",
            "no fold model",
            "predictor error",
            "used rate",
            "terms",
        ),
        [
            (
                _cell(name),
                _cell(block.get("decisions")),
                _cell(block.get("no_fold_model")),
                _cell(block.get("predictor_error")),
                _num(block.get("used_rate")),
                _cell(
                    ", ".join(
                        f"{k} {v}"
                        for k, v in sorted((block.get("terms") or {}).items())
                    )
                    or None
                ),
            )
            for name, block in rows
            if isinstance(block, Mapping)
        ],
    )
    terms = _map(ml.get("terms"))
    if terms:
        lines += ["", "### Fold models' held-out prediction error per term", ""]
        lines += _table(
            (
                "term",
                "rows",
                "MALE model",
                "MALE raw",
                "MAPE model",
                "coverage",
                "usable",
            ),
            [
                (
                    _cell(term),
                    _cell(t.get("rows")),
                    _num(t.get("male_model"), 3),
                    _num(t.get("male_raw"), 3),
                    _num(t.get("mape_model"), 3),
                    _num(t.get("coverage")),
                    _num(t.get("usable_rate")),
                )
                for term, t in sorted(terms.items())
                if isinstance(t, Mapping)
            ],
        )
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    """The Markdown rendering of ``report`` (module docstring)."""
    settings = report.get("settings") or {}
    ci_label = _ci_label(settings.get("ci_level"))
    counts = report.get("counts") or {}
    lines: list[str] = ["# Planner evaluation report", ""]
    if report.get("leaked"):
        lines += [
            "> **LEAKED** - made with `--allow-leak`: the calibrated rows used the",
            "> live calibration, which may have been fitted on these runs. No",
            "> verdict below is a claim; `would be` shows the numbers only.",
            "",
        ]
    lines += [
        f"- eval_version: {_cell(report.get('eval_version'))}",
        "- campaigns: "
        + ", ".join(f"`{_cell(c)}`" for c in report.get("campaign_ids") or ()),
        f"- held-out family: {_cell(report.get('holdout') or 'every family (leave-one-out)')}",
        f"- criterion policy: `{_cell(settings.get('criterion_policy'))}`; threshold "
        f"{_num(settings.get('regret_threshold_pct'), 0)} %, target "
        f"{_num(settings.get('within_target_pct'), 0)} %, at least "
        f"{_cell(settings.get('min_groups'))} groups",
        f"- bootstrap: {_cell(settings.get('bootstrap_resamples'))} resamples, seed "
        f"{_cell(settings.get('bootstrap_seed'))}, level "
        f"{_num(settings.get('ci_level'))}",
        "",
        "## Verdicts",
        "",
    ]
    lines += _table(
        ("criterion", "status", "would be", "value", "threshold", "groups", "detail"),
        [
            (
                _cell(v.get("criterion")),
                _cell(v.get("status")),
                _cell(v.get("would_be")),
                _num(v.get("value")),
                _num(v.get("threshold")),
                _cell(v.get("counted_groups")),
                _cell(v.get("detail")),
            )
            for v in report.get("verdicts") or ()
        ],
    )
    paired = [
        c
        for v in report.get("verdicts") or ()
        if v.get("criterion") == 2
        for c in v.get("comparisons") or ()
    ]
    if paired:  # D322: criterion 2 is paired
        lines += [
            "",
            "### Criterion 2, paired: rule policy regret minus criterion policy "
            "regret, on the groups counted for both",
            "",
        ]
        lines += _table(
            (
                "rule policy",
                "common groups",
                "mean difference (% points)",
                ci_label,
                "unmeasured",
            ),
            [
                (
                    f"`{_cell(c.get('key'))}`",
                    _cell(c.get("common_groups")),
                    _num(c.get("mean_diff_pct")),
                    _DASH
                    if c.get("ci_low") is None
                    else f"[{_num(c.get('ci_low'))}, {_num(c.get('ci_high'))}]",
                    _cell(c.get("unmeasured_groups")),
                )
                for c in paired
            ],
        )
    lines += _latency_lines(report.get("latency"))
    lines += _ml_lines(report.get("ml"))  # V0.15 G5a
    lines += ["", "## Policies (regret vs the best measured configuration)", ""]
    lines += _table(
        (
            "policy",
            "counted",
            "mean regret %",
            ci_label,
            "mean regret ms",
            "within threshold %",
            "zero regret",
            "unmeasured",
            "no replay",
        ),
        [
            (
                f"`{_cell(row.get('key'))}`",
                _cell(row.get("counted_groups")),
                _num(row.get("mean_regret_pct")),
                _ci(row),
                _num(row.get("mean_regret_ms"), 1),
                _num(row.get("within_threshold_pct"), 1),
                _cell(row.get("zero_regret_groups")),
                _cell(row.get("unmeasured_groups")),
                _cell(row.get("no_replay_groups")),
            )
            for row in report.get("policies") or ()
        ],
    )
    shown = {settings.get("criterion_policy"), "oracle"}
    family_rows = [r for r in report.get("families") or () if r.get("key") in shown]
    if family_rows:
        lines += ["", "## Per family (criterion policy and oracle)", ""]
        lines += _table(
            ("family", "policy", "counted", "mean regret %", "within %", "unmeasured"),
            [
                (
                    _cell(r.get("family")),
                    f"`{_cell(r.get('key'))}`",
                    _cell(r.get("counted_groups")),
                    _num(r.get("mean_regret_pct")),
                    _num(r.get("within_threshold_pct"), 1),
                    _cell(r.get("unmeasured_groups")),
                )
                for r in family_rows
            ],
        )
    labels = counts.get("labels") or {}
    skipped = counts.get("records_skipped") or {}
    lines += [
        "",
        "## Counts",
        "",
        f"- groups: {_cell(counts.get('groups'))} (evaluated "
        f"{_cell(counts.get('evaluated_groups'))}, no replay "
        f"{_cell(counts.get('no_replay_groups'))})",
        "- labels: "
        + ", ".join(f"{_cell(k)} {_cell(v)}" for k, v in sorted(labels.items())),
        f"- records: {_cell(counts.get('records'))} (used "
        f"{_cell(counts.get('records_used'))}; skipped: "
        + ", ".join(f"{_cell(k)} {_cell(v)}" for k, v in sorted(skipped.items()))
        + ")",
        "",
        "## Calibrations used by the calibrated rows",
        "",
    ]
    lines += _table(
        ("family", "source", "version", "fitted runs", "held out", "factors"),
        [
            (
                _cell(c.get("family")),
                _cell(c.get("source")),
                _cell(c.get("version")),
                _cell(c.get("fitted_runs")),
                _cell(c.get("excluded_runs")),
                _cell(c.get("factors")),
            )
            for c in report.get("calibrations") or ()
        ],
    )
    excluded = [
        g
        for g in report.get("groups") or ()
        if g.get("status") in ("excluded", "no_replay")
    ]
    if excluded:
        lines += ["", "## Groups not counted", ""]
        lines += _table(
            ("campaign", "group", "family", "status", "why"),
            [
                (
                    _cell(g.get("campaign_id")),
                    _cell(g.get("group_id")),
                    _cell(g.get("family")),
                    _cell(g.get("status")),
                    _cell(g.get("excluded_reason")),
                )
                for g in excluded
            ],
        )
    return "\n".join(lines) + "\n"
