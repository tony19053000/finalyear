"""Raw evaluation data -> canonical analysis + tables (D417-D419).

    uv run python scripts/eval/analyze.py --raw RAW_DIR [--raw RAW_DIR ...] \\
        --out OUT_DIR [--eval-report EVAL.json] [--ml-report REPORT.json]

``--raw`` once per campaign (one per link for A7). ``--eval-report`` = the
planner evaluator report of the L4 data campaign (``compute planner eval
--json`` output as saved - its ``report`` object is taken out of the stored
record - or the bare report); ``--ml-report`` = ``report.json`` of ``compute
planner ml compare --ablations``. Both are optional; the report says what is
missing.

Writes ``OUT_DIR/analysis.json`` (canonical JSON), ``OUT_DIR/tables/*.csv``
and ``OUT_DIR/SHA256SUMS`` (inputs and outputs). The same inputs give the
same bytes (D419; the CI ``eval-repro`` job checks a committed sample).

Exit codes: 0 written; 2 bad arguments or unreadable input.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit.analysis import AnalysisSettings, analyze
from evalkit.canonical import dumps, sha256_bytes, sha256_file, write_text
from evalkit.offline import offline_block
from evalkit.raw import CAMPAIGN_FILE, RUNS_FILE, RawError, read_raw

__all__ = ["main", "run_analysis"]


def _csv(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue()


def tables(analysis: Mapping[str, Any]) -> dict[str, str]:
    """The CSV tables (file name -> text)."""
    arms = list(analysis["arms"])
    group_rows = []
    for group in analysis["groups"]:
        for arm in arms:
            item = group["arms"][arm]
            summary = item["makespan_ms"] or {}
            group_rows.append(
                [
                    group["key"],
                    arm,
                    item["state"],
                    item["runs"],
                    summary.get("median"),
                    summary.get("ci_low"),
                    summary.get("ci_high"),
                    item["regret_pct"],
                    group["oracle"],
                ]
            )
    comparison_rows = [
        [
            c["arm"],
            c["reference"],
            c["kind"],
            c["scope"],
            c["groups"],
            c["speedup_geomean"],
            None if c["speedup_ci"] is None else c["speedup_ci"][0],
            None if c["speedup_ci"] is None else c["speedup_ci"][1],
            c["verdict"],
            c["reason"],
        ]
        for c in analysis["comparisons"]
    ]
    arm_rows = [
        [
            arm,
            row["label"],
            row["groups_measured"],
            row["mean_regret_pct"],
            None if row["regret_ci"] is None else row["regret_ci"][0],
            None if row["regret_ci"] is None else row["regret_ci"][1],
            row["within_threshold_pct"],
        ]
        for arm, row in ((a, analysis["per_arm"][a]) for a in arms)
    ]
    return {
        "groups.csv": _csv(
            (
                "group",
                "arm",
                "state",
                "runs",
                "median_makespan_ms",
                "ci_low_ms",
                "ci_high_ms",
                "regret_pct",
                "oracle",
            ),
            group_rows,
        ),
        "comparisons.csv": _csv(
            (
                "arm",
                "reference",
                "kind",
                "scope",
                "groups",
                "speedup_geomean",
                "speedup_ci_low",
                "speedup_ci_high",
                "verdict",
                "reason",
            ),
            comparison_rows,
        ),
        "arms.csv": _csv(
            (
                "arm",
                "label",
                "groups_measured",
                "mean_regret_pct",
                "regret_ci_low",
                "regret_ci_high",
                "within_threshold_pct",
            ),
            arm_rows,
        ),
    }


def _load_json(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")  # noqa: TRY004
    return value


def _eval_report(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The evaluator report itself. ``compute planner eval --json`` (and
    ``GET /v1/planner/eval-reports/{id}``) wrap it in the stored record
    (``{"id", "state", "report": {...}, ...}``); without unwrapping, every
    field would read as missing and A4 / the evaluator rows would silently be
    empty (V1.0 G6 prep). A bare report is used as given."""
    if value is None:
        return None
    inner = value.get("report")
    if isinstance(inner, dict) and not ({"policies", "groups"} & value.keys()):
        return inner
    return value


def run_analysis(
    raw_dirs: Sequence[Path],
    out: Path,
    *,
    eval_report: Path | None = None,
    ml_report: Path | None = None,
    settings: AnalysisSettings | None = None,
) -> dict[str, str]:
    """Analyse and write everything; ``{relative path: sha256}`` of the
    outputs (the SHA256SUMS content)."""
    settings = settings or AnalysisSettings()
    raws = [read_raw(d) for d in raw_dirs]
    offline = None
    if eval_report is not None or ml_report is not None:
        offline = offline_block(
            _eval_report(_load_json(eval_report)),
            _load_json(ml_report),
            seed=settings.seed,
            resamples=settings.resamples,
            min_groups=settings.min_groups,
        )
    analysis = analyze(raws, settings=settings, offline=offline)
    outputs: dict[str, str] = {}
    text = dumps(analysis)
    write_text(out / "analysis.json", text)
    outputs["analysis.json"] = sha256_bytes(text.encode("utf-8"))
    for name, body in sorted(tables(analysis).items()):
        write_text(out / "tables" / name, body)
        outputs[f"tables/{name}"] = sha256_bytes(body.encode("utf-8"))
    inputs: dict[str, str] = {}
    for raw_dir, raw in zip(raw_dirs, raws, strict=True):
        name = str(raw.campaign["spec"]["name"])
        for file in (CAMPAIGN_FILE, RUNS_FILE):
            if (raw_dir / file).exists():
                inputs[f"input:{name}/{file}"] = sha256_file(raw_dir / file)
    for label, path in (("eval-report", eval_report), ("ml-report", ml_report)):
        if path is not None:
            inputs[f"input:{label}"] = sha256_file(path)
    sums = "".join(
        f"{digest}  {name}\n" for name, digest in sorted({**inputs, **outputs}.items())
    )
    write_text(out / "SHA256SUMS", sums)
    return outputs


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="analyze.py", description=__doc__.split("\n")[0])
    p.add_argument("--raw", type=Path, action="append", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--eval-report", type=Path, default=None)
    p.add_argument("--ml-report", type=Path, default=None)
    defaults = AnalysisSettings()
    p.add_argument("--seed", type=int, default=defaults.seed)
    p.add_argument("--resamples", type=int, default=defaults.resamples)
    args = p.parse_args(argv)
    settings = AnalysisSettings(seed=args.seed, resamples=args.resamples)
    try:
        outputs = run_analysis(
            args.raw,
            args.out,
            eval_report=args.eval_report,
            ml_report=args.ml_report,
            settings=settings,
        )
    except (OSError, RawError, ValueError) as exc:
        print(f"analyze: {exc}", file=sys.stderr)
        return 2
    print(f"analysis.json sha256 {outputs['analysis.json']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
