"""An evaluation report as canonical JSON text and as Markdown (V0.14 G4, D297/D318).

* :func:`report_text` - sorted keys, no whitespace, no NaN: the same report
  gives the same bytes (the determinism test compares them).
* :func:`render_markdown` - the human report: a ``LEAKED`` banner when the
  calibration may have seen the data, the verdicts, the per-policy table
  (mean regret with its CI, share within the threshold, unmeasured and
  no-replay counts), the per-family table of the criterion policy and the
  oracle, the counts and the calibrations used. Every value comes from the
  report; nothing is recomputed, so JSON and Markdown cannot disagree.

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
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


def _ci(row: Mapping[str, Any]) -> str:
    ci = row.get("ci_pct")
    if not isinstance(ci, Mapping) or ci.get("low") is None:
        return _DASH
    return f"[{_num(ci.get('low'))}, {_num(ci.get('high'))}]"


def render_markdown(report: Mapping[str, Any]) -> str:
    """The Markdown rendering of ``report`` (module docstring)."""
    settings = report.get("settings") or {}
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
                str(v.get("criterion")),
                str(v.get("status")),
                _cell(v.get("would_be")),
                _num(v.get("value")),
                _num(v.get("threshold")),
                _cell(v.get("counted_groups")),
                _cell(v.get("detail")),
            )
            for v in report.get("verdicts") or ()
        ],
    )
    lines += ["", "## Policies (regret vs the best measured configuration)", ""]
    lines += _table(
        (
            "policy",
            "counted",
            "mean regret %",
            "95 % CI",
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
