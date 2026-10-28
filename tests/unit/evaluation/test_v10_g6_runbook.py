"""V1.0 G6 prep: the G6 runbook (``docs/briefs/progress/V1.0-G6.md``) is one
``analyze.py`` + one ``make_report.py`` call on the shapes Day L4 of
``lab/checklists/V1.0-campaign.md`` produces:

* three raw folders - the policy matrix (B0-B6, one link) and the ablation
  campaign once per link (B0/B6/A5/A6) - analysed together: comparisons, A5/A6
  and A7 (Wi-Fi vs Ethernet) all come out of the one call;
* the evaluator report exactly as ``compute planner eval --json`` saves it
  (the stored record wrapping ``report``) gives the same analysis as the bare
  report - before the fix the wrapper made every evaluator field read as
  missing, so A4 was silently empty.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy", reason="the paired verdict helper needs numpy")
pytest.importorskip("pyarrow", reason="ml.train.metrics imports the dataset module")

from mf_eval_kit import load, write_raw

load()

import analyze as analyze_script  # noqa: E402
import make_report as make_report_script  # noqa: E402

pytestmark = pytest.mark.unit

MATRIX_ARMS = ("B0", "B1", "B2", "B3", "B4", "B5", "B6")
ABLATION_ARMS = ("B0", "B6", "A5", "A6")


def _makespan(link: str):  # type: ignore[no-untyped-def]
    def fn(family: str, size: str, arm: str, block: int) -> float:
        rng = random.Random(f"{link}|{family}|{size}|{arm}|{block}")  # noqa: S311
        base = 20_000.0 if arm == "B0" else 12_000.0
        if link == "wifi":
            base *= 1.2
        return base * math.exp(rng.gauss(0.0, 0.05))

    return fn


def _eval_report(groups: int) -> dict[str, Any]:
    return {
        "eval_version": 2,
        "campaign_ids": ["camp_n1"],
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
                "campaign_id": "camp_n1",
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
                        "regret_pct": 6.0 + (i % 2) * 0.2,
                    },
                ],
            }
            for i in range(groups)
        ],
    }


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("l4")
    families = {"cpu.primes": ("s", "l"), "split.hash_files": ("s", "l")}
    raws = [
        write_raw(
            root / "v1-matrix",
            name="v1-matrix",
            arms=MATRIX_ARMS,
            families=families,
            repetitions=10,
            makespan=_makespan("ethernet"),
            link="ethernet",
        ),
    ]
    raws += [
        write_raw(
            root / f"v1-ablation-{link}",
            name=f"v1-ablation-{link}",
            arms=ABLATION_ARMS,
            families={"cpu.primes": ("a",), "split.hash_files": ("a",)},
            repetitions=5,
            makespan=_makespan(link),
            link=link,
        )
        for link in ("wifi", "ethernet")
    ]
    report = _eval_report(12)
    bare = root / "eval-bare.json"
    bare.write_text(json.dumps(report), "utf-8")
    wrapped = root / "lab-v1-eval.json"
    wrapped.write_text(
        json.dumps({"id": 7, "state": "done", "report": report, "schema_version": 1}),
        "utf-8",
    )
    return {"root": root, "raws": raws, "bare": bare, "wrapped": wrapped}


def _analyze(lab: dict[str, Any], eval_report: Path, out: Path) -> dict[str, Any]:
    argv = [a for raw in lab["raws"] for a in ("--raw", str(raw))]
    argv += ["--out", str(out), "--eval-report", str(eval_report)]
    assert analyze_script.main(argv) == 0
    loaded: dict[str, Any] = json.loads((out / "analysis.json").read_text("utf-8"))
    return loaded


def test_one_call_gives_comparisons_live_ablations_and_a7(lab: dict[str, Any]) -> None:
    analysis = _analyze(lab, lab["wrapped"], lab["root"] / "out-wrapped")
    assert analysis["links"] == ["ethernet", "wifi"]
    assert set(analysis["arms"]) == {*MATRIX_ARMS, "A5", "A6"}
    compared = {c["arm"] for c in analysis["comparisons"]}
    assert {"B0", "B6", "A5", "A6"} <= compared
    assert analysis["a7"]["status"] == "evaluated"
    offline = analysis["offline"]
    assert offline["evaluator"]["campaign_ids"] == ["camp_n1"]
    assert offline["evaluator"]["policies"], "the wrapped report's rows were lost"
    assert offline["A4"]["groups"] == 12


def test_the_saved_cli_json_and_the_bare_report_analyse_identically(
    lab: dict[str, Any],
) -> None:
    a = lab["root"] / "a"
    b = lab["root"] / "b"
    _analyze(lab, lab["wrapped"], a)
    _analyze(lab, lab["bare"], b)
    assert (a / "analysis.json").read_bytes() == (b / "analysis.json").read_bytes()


def test_the_report_is_written_from_the_one_analysis(lab: dict[str, Any]) -> None:
    out = lab["root"] / "rep"
    _analyze(lab, lab["wrapped"], out)
    md = lab["root"] / "v1.0-evaluation.md"
    argv = ["--analysis", str(out / "analysis.json"), "--out", str(md), "--no-figures"]
    assert make_report_script.main(argv) == 0
    text = md.read_text("utf-8")
    assert "A7" in text and "A4" in text


def test_a_record_with_report_rows_on_top_is_not_unwrapped() -> None:
    bare_with_key = {"policies": [], "groups": [], "report": {"policies": [1]}}
    assert analyze_script._eval_report(bare_with_key) is bare_with_key
    assert analyze_script._eval_report(None) is None
