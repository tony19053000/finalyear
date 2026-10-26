"""V1.0 G2 (D417-D419): the evaluation analysis - verdicts that follow the
data (noise -> ``not_separable``; a real effect -> ``better`` / ``worse``),
regret computed with the offline evaluator's own functions, honest
bookkeeping of left-out runs, and byte-identical output whatever the order
of the input."""

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

from evalkit.analysis import AnalysisSettings, analyze  # noqa: E402
from evalkit.canonical import dumps  # noqa: E402
from evalkit.raw import read_raw  # noqa: E402
from evalkit.report import render_report  # noqa: E402

pytestmark = pytest.mark.unit

FAMILIES = {"cpu.primes": ("s", "m", "l"), "split.hash_files": ("s", "m", "l")}
ARMS = ("B0", "B1", "B5", "B6")


def _noise(seed: int, sigma: float = 0.15) -> Any:
    def makespan(family: str, size: str, arm: str, block: int) -> float:
        rng = random.Random(f"{seed}|{family}|{size}|{arm}|{block}")
        base = {"s": 8000.0, "m": 16000.0, "l": 32000.0}.get(size, 12000.0)
        return base * math.exp(rng.gauss(0.0, sigma))

    return makespan


def _effect(factor: dict[str, float], seed: int = 1) -> Any:
    noise = _noise(seed, sigma=0.05)

    def makespan(family: str, size: str, arm: str, block: int) -> float:
        return noise(family, size, arm, block) * factor.get(arm, 1.0)

    return makespan


def _analysis(tmp_path: Path, makespan: Any, **kwargs: Any) -> dict[str, Any]:
    folder = write_raw(
        tmp_path / "raw",
        name="t",
        arms=kwargs.pop("arms", ARMS),
        families=kwargs.pop("families", FAMILIES),
        repetitions=kwargs.pop("repetitions", 5),
        makespan=makespan,
        smoke=True,
        **kwargs,
    )
    return analyze([read_raw(folder)])


def _verdicts(analysis: dict[str, Any], scope: str = "all") -> dict[str, str]:
    return {
        f"{c['arm']}-{c['reference']}": c["verdict"]
        for c in analysis["comparisons"]
        if c["scope"] == scope
    }


# The evidence design (D417): >= 6 families x 2 sizes, >= 10 repetitions.
EVIDENCE_FAMILIES = {
    "cpu.primes": ("s", "m", "l", "x", "y", "z"),
    "split.hash_files": ("s", "m", "l", "x", "y", "z"),
}


def test_noise_data_is_reported_not_separable(tmp_path: Path) -> None:
    """Every arm drawn from the same distribution: the report says
    ``not_separable`` - no ``better``, no ``worse``."""
    analysis = _analysis(
        tmp_path, _noise(11), families=EVIDENCE_FAMILIES, repetitions=10
    )
    verdicts = _verdicts(analysis)
    assert len(verdicts) == 6, verdicts  # B0, B1, B6 vs B5 + B1, B5, B6 vs B0
    assert set(verdicts.values()) == {"not_separable"}, verdicts
    assert all(c["groups"] == 12 for c in analysis["comparisons"])
    text = render_report(analysis, analysis_sha256="0" * 64, figures=(), figures_dir="f")
    assert "**not_separable**" in text
    assert "**better**" not in text and "**worse**" not in text


def test_noise_rarely_claims_anything_over_many_datasets(tmp_path: Path) -> None:
    """Not one lucky seed: over 40 noise datasets of the evidence design a
    ``better`` claim stays rare (D357's min-effect rule) and ``not_separable``
    is the verdict at least 90 % of the time. (Measured while building G2:
    better 0-1.3 %, worse ~5 %; with only 6 groups and 5 repetitions the
    percentile bootstrap under-covers - better ~6 %, worse ~8 % - which is
    why the evidence design asks for more.)"""
    counts = {"better": 0, "worse": 0, "not_separable": 0}
    for seed in range(40):
        analysis = _analysis(
            tmp_path / str(seed),
            _noise(seed),
            families=EVIDENCE_FAMILIES,
            repetitions=10,
        )
        for verdict in _verdicts(analysis).values():
            counts[verdict] += 1
    total = sum(counts.values())
    assert total == 40 * 6
    assert counts["better"] / total <= 0.03, counts
    assert counts["not_separable"] / total >= 0.90, counts


def test_a_real_speedup_is_better_and_a_slowdown_is_worse(tmp_path: Path) -> None:
    analysis = _analysis(tmp_path, _effect({"B6": 0.7, "B1": 1.3}))
    verdicts = _verdicts(analysis)
    assert verdicts["B6-B5"] == "better"
    assert verdicts["B6-B0"] == "better"
    assert verdicts["B1-B5"] == "worse"
    assert verdicts["B0-B5"] == "not_separable"
    row = next(c for c in analysis["comparisons"] if c["arm"] == "B6" and c["reference"] == "B5")
    low, high = row["speedup_ci"]
    assert low <= row["speedup_geomean"] <= high
    assert 1.3 < row["speedup_geomean"] < 1.6  # 1 / 0.7 = 1.43


def test_too_few_groups_is_never_a_claim(tmp_path: Path) -> None:
    analysis = _analysis(
        tmp_path,
        _effect({"B6": 0.5}),
        families={"cpu.primes": ("s", "m")},
    )
    row = next(c for c in analysis["comparisons"] if c["arm"] == "B6" and c["reference"] == "B5")
    assert row["groups"] == 2
    assert (row["verdict"], row["reason"]) == ("not_separable", "too_few_groups")


def test_regret_matches_the_offline_evaluator_definition(tmp_path: Path) -> None:
    analysis = _analysis(tmp_path, _effect({"B6": 0.7, "B1": 1.3}))
    for group in analysis["groups"]:
        medians = {
            arm: item["makespan_ms"]["median"] for arm, item in group["arms"].items()
        }
        best = min(medians.values())
        assert group["oracle_ms"] == best
        assert group["arms"][group["oracle"]]["regret_pct"] == 0.0
        for arm, median in medians.items():
            expected = round((median - best) / best * 100.0, 6)
            assert group["arms"][arm]["regret_pct"] == pytest.approx(expected, abs=1e-6)
    assert analysis["per_arm"]["B6"]["mean_regret_pct"] < 5.0
    assert analysis["per_arm"]["B1"]["mean_regret_pct"] > 40.0


def test_failed_and_mismatched_runs_are_counted_not_used(tmp_path: Path) -> None:
    def status(family: str, size: str, arm: str, block: int) -> str:
        return "failed" if (arm == "B1" and block == 0) else "ok"

    analysis = _analysis(tmp_path, _noise(3), status=status, ml_version=None)
    assert analysis["per_arm"]["B1"]["statuses"] == {"failed": 6, "ok": 24}
    # B6 planned without any ML model is not the ML planner: all left out
    assert analysis["per_arm"]["B6"]["statuses"] == {"mismatch:ml_inactive": 30}
    assert analysis["per_arm"]["B6"]["groups_measured"] == 0
    row = next(c for c in analysis["comparisons"] if c["arm"] == "B6" and c["reference"] == "B5")
    assert (row["groups"], row["verdict"]) == (0, "not_separable")


def test_fewer_runs_than_min_runs_is_insufficient(tmp_path: Path) -> None:
    analysis = _analysis(tmp_path, _noise(4), repetitions=2)
    assert all(
        item["state"] == "insufficient"
        for group in analysis["groups"]
        for item in group["arms"].values()
    )
    assert all(c["groups"] == 0 for c in analysis["comparisons"])


def test_byte_identical_and_independent_of_input_order(tmp_path: Path) -> None:
    folder = write_raw(
        tmp_path / "raw",
        name="t",
        arms=ARMS,
        families=FAMILIES,
        repetitions=5,
        makespan=_noise(5),
        smoke=True,
    )
    first = dumps(analyze([read_raw(folder)]))
    assert first == dumps(analyze([read_raw(folder)]))
    lines = (folder / "runs.jsonl").read_text("utf-8").splitlines()
    random.Random(1).shuffle(lines)
    (folder / "runs.jsonl").write_text("\n".join(lines) + "\n", "utf-8")
    assert dumps(analyze([read_raw(folder)])) == first


def test_a7_compares_two_links_and_needs_both(tmp_path: Path) -> None:
    def make(link: str) -> Path:
        noise = _noise(8, sigma=0.05)

        def makespan(family: str, size: str, arm: str, block: int) -> float:
            slow = 1.5 if link == "wifi" and arm != "B0" else 1.0
            return noise(family, size, arm, block) * slow

        return write_raw(
            tmp_path / link,
            name=f"t-{link}",
            arms=ARMS,
            families=FAMILIES,
            repetitions=5,
            makespan=makespan,
            link=link,
            smoke=True,
        )

    wifi = read_raw(make("wifi"))
    only = analyze([wifi])
    assert only["a7"]["status"] == "not_run"
    both = analyze([read_raw(make("ethernet")), wifi])
    assert both["a7"]["status"] == "evaluated"
    assert both["a7"]["arms"]["B5"]["verdict"] == "better"  # faster on Ethernet
    assert both["a7"]["arms"]["B0"]["verdict"] == "not_separable"  # local: no network
    scopes = {c["scope"] for c in both["comparisons"]}
    assert scopes == {"all", "ethernet", "wifi"}


def test_evidence_flags_and_banner(tmp_path: Path) -> None:
    analysis = _analysis(tmp_path, _noise(2))
    assert analysis["evidence"] is False
    assert analysis["inputs"][0]["smoke"] is True
    text = render_report(analysis, analysis_sha256="0" * 64, figures=(), figures_dir="f")
    assert "**Not evidence.**" in text and "smoke run" in text


def test_mixed_builds_are_flagged(tmp_path: Path) -> None:
    analysis = _analysis(
        tmp_path,
        _noise(2),
        builds=(
            {"git_sha": "a" * 40, "package_version": "1.0.0rc1", "source": "bundle"},
            {"git_sha": "b" * 40, "package_version": "1.0.0rc2", "source": "bundle"},
        ),
    )
    assert analysis["inputs"][0]["mixed_builds"] is True
    assert len(analysis["inputs"][0]["builds"]) == 2


def test_settings_are_recorded_and_used(tmp_path: Path) -> None:
    folder = write_raw(
        tmp_path / "raw",
        name="t",
        arms=ARMS,
        families=FAMILIES,
        repetitions=5,
        makespan=_noise(6),
        smoke=True,
    )
    settings = AnalysisSettings(resamples=500, seed=99)
    analysis = analyze([read_raw(folder)], settings=settings)
    assert analysis["settings"]["resamples"] == 500
    assert analysis["settings"]["seed"] == 99
    json.dumps(analysis, allow_nan=False)  # every number is finite
