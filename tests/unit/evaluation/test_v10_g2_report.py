"""V1.0 G2 (D418/D419): the committed sample reproduces byte for byte, the
offline ablations A1-A4 / RQ2 are taken from the existing tools' reports,
the report prints what is missing as missing, the figures are deterministic,
and the entry scripts fail with exit 2 on bad input."""

from __future__ import annotations

import filecmp
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy", reason="the paired verdict helper needs numpy")
pytest.importorskip("pyarrow", reason="ml.train.metrics imports the dataset module")

from mf_eval_kit import SAMPLE_DIR, load, write_raw, write_sample

load()

import analyze as analyze_script  # noqa: E402
import check_sample  # noqa: E402
import make_report as make_report_script  # noqa: E402
from evalkit.analysis import analyze  # noqa: E402
from evalkit.offline import offline_block  # noqa: E402
from evalkit.raw import read_raw  # noqa: E402
from evalkit.report import render_report  # noqa: E402

pytestmark = pytest.mark.unit


def test_the_committed_sample_is_the_documented_synthetic_campaign(
    tmp_path: Path,
) -> None:
    """``scripts/eval/sample/raw`` is exactly what ``write_sample`` writes
    (nobody edited the data by hand)."""
    write_sample(tmp_path / "raw")
    for name in ("campaign.json", "runs.jsonl", "sessions.jsonl"):
        assert (tmp_path / "raw" / name).read_bytes() == (
            SAMPLE_DIR / "raw" / name
        ).read_bytes(), name


def test_the_sample_analysis_matches_the_committed_hashes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CI ``eval-repro`` job runs this with figures; here (every OS, no
    matplotlib needed) the analysis files."""
    assert check_sample.main(["--no-figures"]) == 0, capsys.readouterr().err


def test_the_sample_is_noise_and_says_so(tmp_path: Path) -> None:
    analysis = analyze([read_raw(SAMPLE_DIR / "raw")])
    assert {c["verdict"] for c in analysis["comparisons"]} == {"not_separable"}
    assert analysis["evidence"] is False


def test_a_hash_mismatch_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = tmp_path / "expected.sha256"
    text = check_sample.EXPECTED.read_text("utf-8")
    expected.write_text(text.replace(text[:8], "00000000", 1), "utf-8")
    monkeypatch.setattr(check_sample, "EXPECTED", expected)
    assert check_sample.main(["--no-figures"]) == 1
    assert "NOT reproducible" in capsys.readouterr().err


def test_a_checkout_build_is_not_evidence(tmp_path: Path) -> None:
    folder = write_raw(
        tmp_path / "raw",
        name="dev",
        arms=("B0", "B5"),
        families={"cpu.primes": ("s",)},
        repetitions=10,
        makespan=lambda *_: 9000.0,
        builds=(
            {"git_sha": "a" * 40, "package_version": "1.0.0", "source": "checkout"},
        ),
    )
    analysis = analyze([read_raw(folder)])
    assert analysis["inputs"][0]["evidence"] is True  # design-wise enough
    assert analysis["inputs"][0]["release_build"] is False
    assert analysis["evidence"] is False
    text = render_report(
        analysis, analysis_sha256="0" * 64, figures=(), figures_dir="f"
    )
    assert "installed release packages (D406)" in text


# ------------------------------------------------------------------ offline


def _eval_report(groups: int, gap: float) -> dict[str, Any]:
    return {
        "eval_version": 2,
        "campaign_ids": ["c1"],
        "leaked": False,
        "policies": [
            {
                "key": "cost_model:calibrated:min_time",
                "counted_groups": groups,
                "mean_regret_pct": 3.0,
                "ci_pct": {"low": 1.0, "high": 5.0},
                "within_threshold_pct": 90.0,
            }
        ],
        "verdicts": [{"criterion": "1", "status": "pass", "would_be": None}],
        "groups": [
            {
                "campaign_id": "c1",
                "group_id": f"g{i}",
                "picks": [
                    {
                        "key": "cost_model:calibrated:min_time",
                        "status": "counted",
                        "regret_pct": 2.0 + (i % 3) * 0.1,
                    },
                    {
                        "key": "cost_model:uncalibrated:min_time",
                        "status": "counted",
                        "regret_pct": 2.0 + gap + (i % 2) * 0.2,
                    },
                    {"key": "random", "status": "skipped", "regret_pct": None},
                ],
            }
            for i in range(groups)
        ],
    }


def _ml_report() -> dict[str, Any]:
    return {
        "dataset": {"digest": "sha256:" + "d" * 64},
        "terms": [
            {
                "term": "compute",
                "rows": 400,
                "families": 6,
                "status": "compared",
                "pooled": {
                    "male_model": 0.11,
                    "male_calibration": 0.14,
                    "male_raw": 0.4,
                    "mape_model": 12.0,
                    "mape_calibration": 15.0,
                },
                "vs_calibration": {
                    "verdict": "not_separable",
                    "reason": "ci_not_beyond_min_effect",
                    "low": -0.05,
                    "high": 0.01,
                },
            }
        ],
        "ablations": {
            "terms": {
                "compute": [
                    {
                        "group": "hardware",
                        "status": "compared",
                        "families": 6,
                        "mean": 0.08,
                        "low": 0.03,
                        "high": 0.12,
                        "delta_male_pooled": 0.07,
                        "verdict": "matters",
                        "reason": "ci_above_min_effect",
                    },
                    {
                        "group": "load",
                        "status": "compared",
                        "families": 6,
                        "mean": 0.0,
                        "low": -0.01,
                        "high": 0.01,
                        "delta_male_pooled": 0.0,
                        "verdict": "not_separable",
                        "reason": "ci_contains_zero",
                    },
                ]
            }
        },
    }


def test_a4_follows_the_evaluator_groups() -> None:
    matters = offline_block(
        _eval_report(8, gap=4.0), None, seed=1, resamples=500, min_groups=5
    )
    assert matters["A4"]["verdict"] == "matters"
    assert matters["A4"]["groups"] == 8
    assert matters["A4"]["mean_pp"] == pytest.approx(4.0 + 0.1 - 0.1, abs=0.2)
    needless = offline_block(
        _eval_report(8, gap=-3.0), None, seed=1, resamples=500, min_groups=5
    )
    assert needless["A4"]["verdict"] == "not_needed"
    few = offline_block(
        _eval_report(3, gap=4.0), None, seed=1, resamples=500, min_groups=5
    )
    assert (few["A4"]["verdict"], few["A4"]["reason"]) == (
        "not_separable",
        "too_few_groups",
    )
    assert matters["ml_compare"] is None
    assert matters["A1"] == {"status": "not_given"}


def test_a1_a3_and_rq2_come_from_the_ml_report() -> None:
    block = offline_block(None, _ml_report(), seed=1, resamples=500, min_groups=5)
    assert block["A4"] == {"status": "not_given"}
    assert block["A1"]["status"] == "evaluated"
    assert block["A1"]["terms"][0]["verdict"] == "matters"
    assert block["A2"]["terms"][0]["verdict"] == "not_separable"
    # the report has no cache-group row: said, not invented
    assert block["A3"] == {"group": "cache", "status": "not_in_report", "terms": []}
    term = block["ml_compare"]["terms"][0]
    assert (term["male_model"], term["verdict"]) == (0.11, "not_separable")


def test_the_report_prints_offline_results_and_missing_inputs(tmp_path: Path) -> None:
    raw = read_raw(SAMPLE_DIR / "raw")
    none = render_report(
        analyze([raw]), analysis_sha256="0" * 64, figures=(), figures_dir="f"
    )
    assert "_Not given: run `analyze.py` with `--eval-report`" in none
    assert "A7 not evaluated" in none
    block = offline_block(
        _eval_report(8, gap=4.0), _ml_report(), seed=1, resamples=500, min_groups=5
    )
    text = render_report(
        analyze([raw], offline=block),
        analysis_sha256="0" * 64,
        figures=("a.svg",),
        figures_dir="figs",
    )
    assert "| A1 | hardware (compute) | 6 |" in text
    assert "| A3 | cache |" in text and "not_in_report" in text
    assert "calibration off (regret, % points)" in text and "**matters**" in text
    assert "| compute | 400 | 6 | compared | 0.110 | 0.140 | 0.400 |" in text
    assert "![a.svg](figs/a.svg)" in text


# ------------------------------------------------------------------ scripts


def test_analyze_and_make_report_scripts_are_deterministic(tmp_path: Path) -> None:
    for out in ("one", "two"):
        assert (
            analyze_script.main(
                ["--raw", str(SAMPLE_DIR / "raw"), "--out", str(tmp_path / out)]
            )
            == 0
        )
        assert (
            make_report_script.main(
                [
                    "--analysis",
                    str(tmp_path / out / "analysis.json"),
                    "--out",
                    str(tmp_path / out / "report.md"),
                    "--no-figures",
                ]
            )
            == 0
        )
    cmp = filecmp.dircmp(tmp_path / "one", tmp_path / "two")
    assert not cmp.diff_files and not cmp.left_only and not cmp.right_only
    sums = (tmp_path / "one" / "SHA256SUMS").read_text("utf-8")
    assert "input:sample-noise/runs.jsonl" in sums and "analysis.json" in sums


def test_scripts_exit_2_on_bad_input(tmp_path: Path) -> None:
    assert (
        analyze_script.main(["--raw", str(tmp_path), "--out", str(tmp_path / "o")]) == 2
    )
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", "utf-8")
    assert (
        analyze_script.main(
            [
                "--raw",
                str(SAMPLE_DIR / "raw"),
                "--out",
                str(tmp_path / "o"),
                "--eval-report",
                str(bad),
            ]
        )
        == 2
    )
    assert (
        make_report_script.main(
            [
                "--analysis",
                str(tmp_path / "missing.json"),
                "--out",
                str(tmp_path / "r.md"),
            ]
        )
        == 2
    )


def test_figures_are_byte_identical(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib", reason="the evaluation dependency group")
    from evalkit.figures import write_figures

    analysis = json.loads(json.dumps(analyze([read_raw(SAMPLE_DIR / "raw")])))
    first = write_figures(analysis, tmp_path / "a")
    second = write_figures(analysis, tmp_path / "b")
    assert first == second and len(first) == 5
    for name in first:
        data = (tmp_path / "a" / name).read_bytes()
        assert data == (tmp_path / "b" / name).read_bytes(), name
        assert b"<dc:date>" not in data  # no timestamp in the SVG


def test_figures_skip_what_the_data_cannot_show(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib", reason="the evaluation dependency group")
    from evalkit.figures import write_figures

    folder = write_raw(
        tmp_path / "raw",
        name="tiny",
        arms=("B1",),
        families={"cpu.primes": ("s",)},
        repetitions=1,
        makespan=lambda *_: 9000.0,
        smoke=True,
    )
    analysis = json.loads(json.dumps(analyze([read_raw(folder)])))
    # one arm, one run: no comparison, no regret CI - only the bar charts
    assert write_figures(analysis, tmp_path / "f") == [
        "makespan_by_group.svg",
        "overhead_by_mode.svg",
    ]
