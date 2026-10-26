"""The analysis document -> the Markdown evaluation report (D419).

Prints what the data says, never more: a campaign that is a smoke run,
incomplete, under ten repetitions or from mixed builds gets a **not
evidence** banner; every verdict is the analysis's own (``better`` /
``not_separable`` / ``worse``, ``matters`` / ``not_needed`` /
``not_separable``); a missing input is printed as missing. Deterministic:
pure string building from the document, fixed number formats.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from evalkit.arms import ARMS

__all__ = ["RESEARCH_QUESTION", "render_report"]

RESEARCH_QUESTION: Final = (
    "On a LAN of heterogeneous personal laptops, does a planner that chooses "
    "between LOCAL, Mode 1 and Mode 3 using calibrated and learned time "
    "predictions reduce job completion time compared with a fixed local "
    "baseline and conventional schedulers, and by how much?"
)
_DASH: Final = "-"


def _n(value: Any, digits: int = 2) -> str:
    if value is None or isinstance(value, bool):
        return _DASH
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _s(ms: Any) -> str:
    return _DASH if not isinstance(ms, int | float) else f"{ms / 1000.0:.2f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return ["_(no rows)_"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return lines


def _ci(pair: Any, digits: int = 2) -> str:
    if not isinstance(pair, list | tuple) or len(pair) != 2 or pair[0] is None:
        return _DASH
    return f"[{_n(pair[0], digits)}, {_n(pair[1], digits)}]"


def _banner(analysis: Mapping[str, Any]) -> list[str]:
    if analysis.get("evidence"):
        return []
    reasons = []
    for item in analysis.get("inputs", ()):
        if item["smoke"]:
            reasons.append(f"`{item['name']}` is a smoke run (sizes below the window)")
        if item["repetitions"] < 10:
            reasons.append(
                f"`{item['name']}` has {item['repetitions']} repetitions (< 10, D417)"
            )
        if not item["complete"]:
            reasons.append(
                f"`{item['name']}` is incomplete ({item['runs_recorded']} of "
                f"{item['runs_planned']} runs)"
            )
        if item["mixed_builds"]:
            reasons.append(f"`{item['name']}` ran on more than one build")
    return [
        "> **Not evidence.** This report is generated from data that cannot",
        "> support a claim: " + "; ".join(reasons) + ".",
        "> The numbers are shown so the pipeline can be checked; no verdict",
        "> below is a result of the project.",
        "",
    ]


def _inputs(analysis: Mapping[str, Any]) -> list[str]:
    rows = []
    for item in analysis.get("inputs", ()):
        builds = ", ".join(
            f"{b.get('package_version')} @ {str(b.get('git_sha'))[:12]}"
            for b in item["builds"]
        )
        rows.append(
            [
                f"`{item['name']}`",
                item["link"],
                str(item["repetitions"]),
                ", ".join(item["arms"]),
                ", ".join(f"`{f}`" for f in item["families"]),
                f"{item['runs_recorded']}/{item['runs_planned']}",
                ", ".join(f"{k} {v}" for k, v in item["statuses"].items()),
                builds or _DASH,
                f"`{item['spec_digest'][:19]}`",
            ]
        )
    return _table(
        (
            "campaign",
            "link",
            "reps",
            "arms",
            "families",
            "runs",
            "statuses",
            "build",
            "spec",
        ),
        rows,
    )


def _arms(analysis: Mapping[str, Any]) -> list[str]:
    per_arm = analysis.get("per_arm", {})
    rows = []
    for arm in analysis.get("arms", ()):
        row = per_arm[arm]
        modes = ", ".join(f"{k} {v}" for k, v in row["modes"].items()) or _DASH
        skipped = {k: v for k, v in row["statuses"].items() if k != "ok"}
        rows.append(
            [
                arm,
                row["label"],
                row["profile"],
                str(row["groups_measured"]),
                _n(row["mean_regret_pct"]),
                _ci(row["regret_ci"]),
                _n(row["within_threshold_pct"], 1),
                modes,
                ", ".join(f"{k} {v}" for k, v in skipped.items()) or _DASH,
            ]
        )
    return _table(
        (
            "arm",
            "what",
            "profile",
            "groups",
            "mean regret %",
            "95 % CI",
            f"within {_n(analysis['settings']['regret_threshold_pct'], 0)} %",
            "modes chosen",
            "runs left out",
        ),
        rows,
    )


def _comparisons(
    analysis: Mapping[str, Any], *, kind: str, scope: str | None
) -> list[str]:
    rows = []
    for c in analysis.get("comparisons", ()):
        if c["kind"] != kind or (scope is not None and c["scope"] != scope):
            continue
        rows.append(
            [
                f"{c['arm']} vs {c['reference']}",
                c["scope"],
                str(c["groups"]),
                _n(c["speedup_geomean"], 3),
                _ci(c["speedup_ci"], 3),
                _n(c["speedup_median"], 3),
                f"**{c['verdict']}**",
                c["reason"],
            ]
        )
    return _table(
        (
            "comparison",
            "link",
            "groups",
            "speedup (geo-mean)",
            "95 % CI",
            "median speedup",
            "verdict",
            "why",
        ),
        rows,
    )


def _groups(analysis: Mapping[str, Any]) -> list[str]:
    arms = list(analysis.get("arms", ()))
    rows = []
    for group in analysis.get("groups", ()):
        cells = []
        for arm in arms:
            item = group["arms"][arm]
            summary = item["makespan_ms"]
            if item["state"] != "measured" or summary is None:
                cells.append(f"({item['runs']} runs)")
            else:
                cells.append(f"{_s(summary['median'])} ({_n(item['regret_pct'], 1)} %)")
        rows.append([f"`{group['key']}`", group["oracle"] or _DASH, *cells])
    return _table(("group", "oracle", *arms), rows)


def _overhead(analysis: Mapping[str, Any]) -> list[str]:
    rows = []
    per_arm = analysis.get("per_arm", {})
    for arm in analysis.get("arms", ()):
        for mode, summary in per_arm[arm]["overhead_ms_by_mode"].items():
            if summary is None:
                continue
            rows.append(
                [
                    arm,
                    mode,
                    str(summary["n"]),
                    _s(summary["median"]),
                    f"[{_s(summary['ci_low'])}, {_s(summary['ci_high'])}]",
                ]
            )
    return _table(("arm", "mode", "jobs", "median overhead (s)", "median CI (s)"), rows)


def _offline(analysis: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """``(RQ2 lines, A1-A4 lines)``."""
    block = analysis.get("offline") or {}
    if block.get("status") == "not_given":
        missing = [
            "_Not given: run `analyze.py` with `--eval-report` (the planner "
            "evaluator report of the L4 data campaign) and `--ml-report` (the "
            "`compute planner ml compare --ablations` report)._"
        ]
        return missing, missing
    rq2: list[str] = []
    ml = block.get("ml_compare")
    if ml is None:
        rq2.append("_ML comparison report not given._")
    else:
        rq2 += _table(
            (
                "term",
                "rows",
                "families",
                "status",
                "MALE model",
                "MALE calibration",
                "MALE raw",
                "verdict vs calibration",
                "CI",
            ),
            [
                [
                    str(t["term"]),
                    _n(t["rows"]),
                    _n(t["families"]),
                    str(t["status"]),
                    _n(t["male_model"], 3),
                    _n(t["male_calibration"], 3),
                    _n(t["male_raw"], 3),
                    f"**{t['verdict']}**",
                    _ci(t["ci"], 3),
                ]
                for t in ml["terms"]
            ],
        )
    evaluator = block.get("evaluator")
    if evaluator is not None:
        rq2 += ["", "Planner evaluator (regret on the data campaign's groups):", ""]
        rq2 += _table(
            ("policy", "groups", "mean regret %", "95 % CI"),
            [
                [
                    f"`{p['key']}`",
                    _n(p["counted_groups"]),
                    _n(p["mean_regret_pct"]),
                    _ci([p["ci_low"], p["ci_high"]]),
                ]
                for p in evaluator["policies"]
            ],
        )
    ablation_rows = []
    for label in ("A1", "A2", "A3"):
        item = block.get(label) or {}
        if item.get("status") != "evaluated":
            ablation_rows.append(
                [
                    label,
                    str(item.get("group", _DASH)),
                    _DASH,
                    _DASH,
                    _DASH,
                    str(item.get("status")),
                ]
            )
            continue
        ablation_rows.extend(
            [
                label,
                f"{item['group']} ({row['term']})",
                _n(row["families"]),
                _n(row["delta_male_mean"], 4),
                _ci(row["ci"], 4),
                f"**{row['verdict']}**",
            ]
            for row in item["terms"]
        )
    a4 = block.get("A4") or {}
    if a4.get("status") == "evaluated":
        ablation_rows.append(
            [
                "A4",
                "calibration off (regret, % points)",
                _n(a4["groups"]),
                _n(a4["mean_pp"]),
                _ci(a4["ci"]),
                f"**{a4['verdict']}**",
            ]
        )
    else:
        ablation_rows.append(
            ["A4", "calibration off", _DASH, _DASH, _DASH, str(a4.get("status"))]
        )
    lines = _table(
        (
            "ablation",
            "removed",
            "families / groups",
            "mean delta",
            "95 % CI",
            "verdict",
        ),
        ablation_rows,
    )
    return rq2, lines


def _a7(analysis: Mapping[str, Any]) -> list[str]:
    block = analysis.get("a7") or {}
    if block.get("status") != "evaluated":
        return [f"_A7 not evaluated: {block.get('reason')}._"]
    rows = [
        [
            arm,
            str(c["groups"]),
            _n(c["speedup_geomean"], 3),
            _ci(c["speedup_ci"], 3),
            f"**{c['verdict']}**",
        ]
        for arm, c in block["arms"].items()
    ]
    return [
        "Per arm, Ethernet vs Wi-Fi on the same (family, size); speedup > 1 = "
        "faster on Ethernet.",
        "",
        *_table(("arm", "groups", "speedup", "95 % CI", "verdict"), rows),
    ]


def render_report(
    analysis: Mapping[str, Any],
    *,
    analysis_sha256: str,
    figures: Sequence[str],
    figures_dir: str,
) -> str:
    """The Markdown report (module docstring)."""
    settings = analysis["settings"]
    links = list(analysis.get("links", ()))
    rq2, ablations = _offline(analysis)
    lines: list[str] = ["# MeshForge v1.0 - evaluation report", ""]
    lines += _banner(analysis)
    lines += [
        "## Research question",
        "",
        f"> {RESEARCH_QUESTION}",
        "",
        "RQ1 completion time vs baselines; RQ2 prediction error; RQ3 which "
        "components matter (ablations); RQ4 the fabric's own overhead.",
        "",
        "## Arms",
        "",
    ]
    lines += _table(
        ("arm", "what", "controller profile"),
        [[a, ARMS[a].label, ARMS[a].profile] for a in analysis.get("arms", ())],
    )
    lines += ["", "## Data", ""]
    lines += _inputs(analysis)
    lines += [
        "",
        f"Analysis: paired bootstrap, {settings['resamples']} resamples, seed "
        f"{settings['seed']}, {round(settings['level'] * 100)} % CI; "
        f"`better` needs >= {settings['min_groups']} paired groups and the CI "
        f"below -{settings['min_effect_log']} in log time (D357); an arm needs "
        f">= {settings['min_runs']} usable runs in a group. Metric: makespan "
        "(submit of the first job to the last result, controller clock).",
        "",
        "## RQ1 - completion time",
        "",
    ]
    lines += _arms(analysis)
    lines += ["", "### Against the baselines (all links pooled)", ""]
    lines += _comparisons(analysis, kind="baseline", scope="all")
    if len(links) > 1:
        lines += ["", "### Per link", ""]
        lines += _comparisons(analysis, kind="baseline", scope=None)
    lines += [
        "",
        "### Per group (median makespan s, regret % vs the group's best arm)",
        "",
    ]
    lines += _groups(analysis)
    lines += ["", "## RQ2 - prediction error (offline, L4 data campaign)", ""]
    lines += rq2
    lines += ["", "## RQ3 - ablations", "", "### A1-A4 (offline)", ""]
    lines += ablations
    lines += ["", "### A5 / A6 (live: a mode disabled in the planner)", ""]
    lines += _comparisons(analysis, kind="ablation", scope=None)
    lines += ["", "### A7 (link)", ""]
    lines += _a7(analysis)
    lines += ["", "## RQ4 - fabric overhead", ""]
    lines += _overhead(analysis)
    if figures:
        lines += ["", "## Figures", ""]
        lines += [f"![{name}]({figures_dir}/{name})" for name in figures]
    lines += [
        "",
        "## Reproduce",
        "",
        "```",
        "uv run python scripts/eval/analyze.py --raw <raw folder>... --out <dir>",
        "uv run python scripts/eval/make_report.py --analysis <dir>/analysis.json "
        "--out <report.md>",
        "```",
        "",
        f"analysis.json sha256: `{analysis_sha256}`",
        "",
    ]
    return "\n".join(lines)
