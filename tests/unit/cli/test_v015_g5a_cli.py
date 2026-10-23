"""V0.15 G5a: the CLI side of the ML evaluation.

* ``compute planner eval`` prints the ML criteria (D370) of a version 2
  report - each verdict, criterion 2's families, how often ML was used -
  ``ml_unavailable`` with its reason, and nothing for a version 1 report;
* ``compute planner ml compare --ablations`` passes the flag to the child and
  prints the ablation table (D372); without the flag no flag, no table.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from meshforge.cli import planner_commands as pc
from meshforge.cli.app import app

pytestmark = pytest.mark.unit

ML_BLOCK: dict[str, Any] = {
    "status": "evaluated",
    "reason": None,
    "verdicts": [
        {
            "name": "ml_criterion_1",
            "status": "PASS",
            "would_be": None,
            "detail": "ML regret minus calibrated regret: CI [-9.0, -4.0] % < 0",
            "families": [],
        },
        {
            "name": "ml_criterion_2",
            "status": "PASS",
            "would_be": None,
            "detail": "every family within",
            "families": [
                {
                    "family": "cpu.matmul",
                    "common_groups": 6,
                    "mean_regret_ml_pct": 1.5,
                    "mean_regret_calibrated_pct": 4.25,
                    "status": "pass",
                },
                {
                    "family": "cpu.sort",
                    "common_groups": 2,
                    "mean_regret_ml_pct": 9.0,
                    "mean_regret_calibrated_pct": 1.0,
                    "status": "too_few_groups",
                },
            ],
        },
    ],
    "fallback": {
        "total": {
            "decisions": 40,
            "no_fold_model": 8,
            "predictor_error": 1,
            "terms": {"used": 30, "ood": 6},
            "used_rate": 0.833333,
        },
        "families": [],
    },
}


def test_eval_lines_print_the_ml_criteria() -> None:
    lines = pc.eval_lines({"id": 3, "report": {"eval_version": 2, "ml": ML_BLOCK}})
    text = "\n".join(lines)
    assert "ml_criterion_1: PASS - ML regret minus calibrated regret" in text
    assert "  cpu.matmul: ML 1.50 % vs calibrated 4.25 % over 6 groups - pass" in text
    assert "cpu.sort" in text and "too_few_groups" in text
    assert "used rate 0.83 (ood 6, used 30)" in text
    assert "without a fold model 8, predictor errors 1" in text


def test_eval_lines_ml_unavailable_and_version_1() -> None:
    unavailable = {
        "status": "ml_unavailable",
        "reason": "[controller.planner] ml = off",
        "verdicts": [
            {"name": "ml_criterion_1", "status": "ml_unavailable", "detail": "x"},
            {"name": "ml_criterion_2", "status": "ml_unavailable", "detail": "x"},
        ],
        "fallback": ML_BLOCK["fallback"],
    }
    text = "\n".join(pc.ml_eval_lines(unavailable))
    assert "ML: ml_unavailable - [controller.planner] ml = off" in text
    assert "ml_criterion_1: ml_unavailable" in text
    assert "used rate" not in text  # nothing ran
    leaked = dict(ML_BLOCK)
    leaked["verdicts"] = [
        {**ML_BLOCK["verdicts"][0], "status": "leaked", "would_be": "PASS"}
    ]
    assert "ml_criterion_1: leaked (would be PASS)" in "\n".join(
        pc.ml_eval_lines(leaked)
    )
    assert pc.ml_eval_lines(None) == []
    assert not any("ml_criterion" in line for line in pc.eval_lines({"report": {}}))


REPORT: dict[str, Any] = {
    "report_version": 2,
    "dataset": {"digest": "sha256:" + "ab" * 32, "rows": 600},
    "terms": [],
    "ablations": {
        "groups": ["load"],
        "terms": {
            "compute": [
                {
                    "group": "load",
                    "status": "evaluated",
                    "families": 5,
                    "delta_male_pooled": 0.2,
                    "mean": 0.19,
                    "low": 0.1,
                    "high": 0.3,
                    "verdict": "matters",
                },
                {"group": "family", "status": "skipped", "reason": "not_in_full_model"},
            ]
        },
    },
}


class _Child:
    def __init__(self) -> None:
        self.argv: list[str] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        report = REPORT if "--ablations" in argv else {**REPORT, "ablations": None}
        (out / "report.json").write_text(json.dumps(report))
        (out / "report.md").write_text("# ML comparison report\n")
        (out / "timings.json").write_text(json.dumps({"total_s": 1.0}))
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.mark.parametrize("flag", [True, False])
def test_compare_ablations_flag_and_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: bool
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    child = _Child()
    monkeypatch.setattr("meshforge.cli.planner_commands.subprocess.run", child)
    monkeypatch.setattr(pc, "ml_missing_libraries", list)
    dataset = tmp_path / "d.parquet"
    dataset.write_bytes(b"PAR1")
    argv = ["planner", "ml", "compare", "--dataset", str(dataset)]
    result = CliRunner().invoke(app, [*argv, "--ablations"] if flag else argv)
    assert result.exit_code == 0, result.output
    assert ("--ablations" in child.argv) is flag
    assert ("Feature-group ablations" in result.stdout) is flag
    if flag:
        assert "matters" in result.stdout and "not in full model" in result.stdout


def test_the_gate_regret_text_old_and_new() -> None:
    from meshforge.cli.ml_model_commands import regret_text

    assert regret_text("pending_g4") == "pending_g4"  # G3 rows still read
    passed = {
        "status": "passed",
        "groups": 12,
        "regret_candidate_pct": 1.5,
        "regret_current_pct": 2.0,
        "current": {"kind": "set", "set_version": "sha256:abc"},
    }
    assert regret_text(passed) == (
        "passed (candidate 1.5 % vs sha256:abc 2.0 % over 12 groups)"
    )
    skipped = {
        "status": "skipped",
        "groups": 3,
        "regret_candidate_pct": None,
        "reason": "regret check skipped (too_few_groups: 3 < 10)",
    }
    assert regret_text(skipped).startswith("skipped (regret check skipped")
    assert regret_text({"status": "not_run"}) == "not_run"
