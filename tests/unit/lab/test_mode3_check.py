"""``lab/mode3_check.py`` + ``lab/_mode3/analysis.py`` (V0.10 G9, roadmap 0.10.10).

The criteria 2-3 arithmetic of D274 on hand-made telemetry rows, and the
``build`` / ``verify`` / ``report`` commands end to end in-process (a real
job folder of the benchmark library, its real whole-output check).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from _mode3.analysis import FAIL, INCONCLUSIVE, PASS, REPORTED, evaluate
from meshforge.bench.workloads import get_family
from mode3_check import files_digest, main

SPLIT = "job_split"
NODES = ["n_a", "n_b", "n_c"]


def _job_row(job: str, node: str, **split: Any) -> dict[str, Any]:
    return {
        "job_id": job,
        "node_id": node,
        "attempt_role": "combine",
        "failure_class": "ok",
        **split,
    }


def _lane(job: str, node: str, stage_ms: int, env_ms: int) -> dict[str, Any]:
    return {
        "job_id": job,
        "node_id": node,
        "attempt_role": "lane",
        "failure_class": "ok",
        "stage_ms": stage_ms,
        "env_ms": env_ms,
    }


def _chunk(
    job: str, node: str, items: int, run_ms: int, won: bool = True
) -> dict[str, Any]:
    return {
        "job_id": job,
        "node_id": node,
        "items": items,
        "run_ms": run_ms,
        "won": won,
    }


def _rows(
    *, single_s: float, split_s: float, makespan_s: float, rates: list[float]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """3 single-node runs (one per node, rate r items/s over 3000 items) and one
    split job over the three nodes."""
    runs: list[dict[str, Any]] = [
        _job_row(
            SPLIT,
            "n_a",
            split_node_set=json.dumps(NODES),
            split_items=3000,
            split_makespan_ms=int(makespan_s * 1000),
            split_job_ms=int(split_s * 1000),
            split_combine_ms=1000,
            split_ideal_bound_s=12.5,
        ),
        _lane(SPLIT, "n_a", 1000, 1000),
        _lane(SPLIT, "n_b", 3000, 1000),  # the slowest start: 4 s
        _lane(SPLIT, "n_c", 500, 0),
    ]
    chunks: list[dict[str, Any]] = []
    singles = []
    for node, rate in zip(NODES, rates, strict=True):
        job = f"job_single_{node}"
        singles.append(job)
        seconds = single_s if rate == max(rates) else single_s * max(rates) / rate
        runs.append(_job_row(job, "n_a", split_job_ms=int(seconds * 1000)))
        chunks.append(_chunk(job, node, 3000, int(3000 / rate * 1000)))
        chunks.append(_chunk(job, "n_x", 999, 1, won=False))  # a loser: ignored
    return runs, chunks, singles


def test_bound_ratio_and_speedup_follow_d274() -> None:
    runs, chunks, singles = _rows(
        single_s=400, split_s=150, makespan_s=140, rates=[10.0, 10.0, 5.0]
    )
    got = evaluate(runs, chunks, split_job_id=SPLIT, single_job_ids=singles)
    assert got.problems == ()
    assert got.rates == pytest.approx({"n_a": 10.0, "n_b": 10.0, "n_c": 5.0})
    assert got.sum_rate == pytest.approx(25.0)
    assert got.rate_ratio == pytest.approx(2.5)
    # 3000 / 25 + the slowest lane start (4 s) + the combine (1 s)
    assert got.ideal_bound_s == pytest.approx(125.0)
    assert got.recorded_bound_s == 12.5
    assert got.makespan_ratio == pytest.approx(140 / 125)
    assert got.c2 == PASS  # best single 400 s >= 300 s: judged, 1.12 <= 1.25
    assert got.best_single_s == pytest.approx(400) and got.best_single_node == "n_a"
    assert got.speedup == pytest.approx(400 / 150)
    assert got.c3 == PASS  # 2.67 >= 0.8 x 2.5 and >= 2.0 on the trio


def test_short_runs_are_reported_not_judged_and_a_slow_split_fails() -> None:
    runs, chunks, singles = _rows(
        single_s=60, split_s=50, makespan_s=60, rates=[10.0, 10.0, 10.0]
    )
    got = evaluate(runs, chunks, split_job_id=SPLIT, single_job_ids=singles)
    assert got.c2 == REPORTED  # best single 60 s < 300 s
    assert got.rate_ratio == pytest.approx(3.0)
    assert got.c3 == FAIL  # 1.2 < 0.8 x 3.0


def test_trio_rule_needs_two_x_when_the_rates_allow_it() -> None:
    # rate_ratio 2.5 on 3 nodes: speedup must reach max(0.8 x 2.5, 2.0) = 2.0
    runs, chunks, singles = _rows(
        single_s=400, split_s=200, makespan_s=150, rates=[10.0, 10.0, 5.0]
    )
    assert evaluate(runs, chunks, split_job_id=SPLIT, single_job_ids=singles).c3 == PASS
    runs, chunks, singles = _rows(
        single_s=400, split_s=210, makespan_s=150, rates=[10.0, 10.0, 5.0]
    )
    assert evaluate(runs, chunks, split_job_id=SPLIT, single_job_ids=singles).c3 == FAIL


def test_a_missing_single_run_is_inconclusive_never_a_pass() -> None:
    runs, chunks, singles = _rows(
        single_s=400, split_s=150, makespan_s=140, rates=[10.0, 10.0, 5.0]
    )
    got = evaluate(runs, chunks, split_job_id=SPLIT, single_job_ids=singles[:2])
    assert (got.c2, got.c3) == (INCONCLUSIVE, INCONCLUSIVE)
    assert any("n_c" in p for p in got.problems), got.problems
    nothing = evaluate(runs, chunks, split_job_id="job_nope", single_job_ids=singles)
    assert (nothing.c2, nothing.c3) == (INCONCLUSIVE, INCONCLUSIVE)
    assert nothing.problems


def test_build_then_verify_a_real_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    params, seed = {"images": 12}, 4
    dest = tmp_path / "job"
    assert (
        main(
            [
                "build",
                "split.image_classify",
                str(dest),
                "--param",
                "images=12",
                "--seed",
                "4",
            ]
        )
        == 0
    )
    assert (dest / "compute.yaml").is_file() and (dest / "run_chunk.py").is_file()
    assert "data/img/*.ppm" in capsys.readouterr().out

    family = get_family("split.image_classify")
    expected = family.expected_output(params, seed, family.input_files(params, seed))  # type: ignore[attr-defined]
    out = tmp_path / "out"
    for name, data in expected.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)
    args = [
        "verify",
        "split.image_classify",
        str(out),
        "--param",
        "images=12",
        "--seed",
        "4",
    ]
    assert main(args) == 0
    text = capsys.readouterr().out
    assert "OK" in text and files_digest(expected) in text

    name = sorted(expected)[0]
    broken = bytearray(expected[name])
    broken[-2] ^= 1
    (out / name).write_bytes(bytes(broken))
    assert main(args) == 3
    assert "PROBLEM" in capsys.readouterr().out
    assert (
        main(
            [
                "verify",
                "split.image_classify",
                str(out),
                "--param",
                "images=12",
                "--seed",
                "5",
            ]
        )
        == 3
    )


def test_usage_errors(tmp_path: Path) -> None:
    assert main(["build", "cpu.nope", str(tmp_path / "a")]) == 2
    assert main(["build", "cpu.matmul", str(tmp_path / "b")]) == 2  # not a split family
    assert (
        main(
            [
                "build",
                "split.image_classify",
                str(tmp_path / "c"),
                "--param",
                "images=5",
            ]
        )
        == 2
    )
    assert (
        main(
            ["verify", "split.image_classify", str(tmp_path / "missing"), "--seed", "1"]
        )
        == 2
    )


def test_report_reads_the_two_exports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    runs, chunks, singles = _rows(
        single_s=400, split_s=150, makespan_s=140, rates=[10.0, 10.0, 5.0]
    )
    keys = sorted({k for r in runs for k in r})
    pq.write_table(
        pa.Table.from_pylist([{k: r.get(k) for k in keys} for r in runs]),
        tmp_path / "runs.parquet",
    )
    pq.write_table(pa.Table.from_pylist(chunks), tmp_path / "chunks.parquet")
    argv = [
        "report",
        str(tmp_path / "runs.parquet"),
        str(tmp_path / "chunks.parquet"),
        "--split",
        SPLIT,
    ]
    for job in singles:
        argv += ["--single", job]
    assert main([*argv, "--out", str(tmp_path / "r.json")]) == 0
    assert "c2: PASS" in capsys.readouterr().out
    doc = json.loads((tmp_path / "r.json").read_text())
    assert doc["c3"] == PASS and doc["node_set"] == NODES
    assert main(argv[:-2]) == 4  # one single-node run left out: INCONCLUSIVE
