"""V0.10 G9 tester: ``lab/mode3_check.py report`` / ``analysis.evaluate`` fed garbage.

Rule under test: a missing, garbled, zero, negative or single-laptop input must
never produce a PASS (INCONCLUSIVE / FAIL / REPORTED are the allowed outcomes).
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import pytest
from test_mode3_check import SPLIT, _rows

from _mode3.analysis import PASS, evaluate
from mode3_check import main


def _good() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    return _rows(single_s=400, split_s=150, makespan_s=140, rates=[10.0, 10.0, 5.0])


def _ev(runs: Any, chunks: Any, singles: Any) -> Any:
    return evaluate(runs, chunks, split_job_id=SPLIT, single_job_ids=singles)


def test_baseline_is_a_pass() -> None:
    got = _ev(*_good())
    assert (got.c2, got.c3) == (PASS, PASS)


def _split_row(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        r for r in runs if r["job_id"] == SPLIT and r["attempt_role"] == "combine"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("split_makespan_ms", 0),
        ("split_makespan_ms", -140000),
        ("split_makespan_ms", None),
        ("split_makespan_ms", float("nan")),
        ("split_items", 0),
        ("split_items", -3000),
        ("split_items", None),
        ("split_combine_ms", -10_000_000),
    ],
)
def test_garbled_c2_inputs_never_pass_c2(field: str, value: Any) -> None:
    runs, chunks, singles = copy.deepcopy(_good())
    _split_row(runs)[field] = value
    got = _ev(runs, chunks, singles)
    assert got.c2 != PASS, (field, value, got)


@pytest.mark.parametrize("value", [0, -150000, None, float("nan")])
def test_garbled_split_job_time_never_passes_c3(value: Any) -> None:
    runs, chunks, singles = copy.deepcopy(_good())
    _split_row(runs)["split_job_ms"] = value
    got = _ev(runs, chunks, singles)
    assert got.c3 != PASS, (value, got)


@pytest.mark.parametrize("value", [0, -150000, float("nan")])
def test_garbled_split_job_time_does_not_pass_c2_either(value: Any) -> None:
    """A row whose job time is garbage is not a trustworthy row (G9-D3, fixed
    in fix round 1)."""
    runs, chunks, singles = copy.deepcopy(_good())
    _split_row(runs)["split_job_ms"] = value
    assert _ev(runs, chunks, singles).c2 != PASS


@pytest.mark.parametrize("run_ms", [0, -1000, None, float("nan")])
def test_bad_single_node_chunk_times_never_pass(run_ms: Any) -> None:
    runs, chunks, singles = copy.deepcopy(_good())
    for c in chunks:
        if c["job_id"] == singles[2] and c["won"]:
            c["run_ms"] = run_ms
    got = _ev(runs, chunks, singles)
    assert got.c2 != PASS and got.c3 != PASS, got


@pytest.mark.parametrize("ms", [0, -400000, None, float("nan")])
def test_bad_single_job_times_never_pass(ms: Any) -> None:
    runs, chunks, singles = copy.deepcopy(_good())
    for r in runs:
        if r["job_id"] == singles[0]:
            r["split_job_ms"] = ms
    got = _ev(runs, chunks, singles)
    assert got.c3 != PASS, got


def test_single_laptop_split_is_inconclusive() -> None:
    runs, chunks, singles = copy.deepcopy(_good())
    _split_row(runs)["split_node_set"] = '["n_a"]'
    got = _ev(runs, chunks, singles)
    assert (got.c2, got.c3) != (PASS, PASS) and got.problems


def test_only_one_single_run_given_is_inconclusive() -> None:
    runs, chunks, singles = _good()
    got = _ev(runs, chunks, singles[:1])
    assert got.c2 == "INCONCLUSIVE" and got.c3 == "INCONCLUSIVE", got


def test_no_rows_at_all_is_inconclusive() -> None:
    got = _ev([], [], [])
    assert got.c2 == "INCONCLUSIVE" and got.c3 == "INCONCLUSIVE"


def test_split_job_id_listed_as_its_own_single_run_does_not_pass() -> None:
    runs, chunks, _ = _good()
    got = _ev(runs, chunks, [SPLIT])
    assert got.c3 != PASS, got


def test_garbled_node_set_is_inconclusive() -> None:
    runs, chunks, singles = copy.deepcopy(_good())
    _split_row(runs)["split_node_set"] = "not json{"
    got = _ev(runs, chunks, singles)
    assert got.c2 != PASS and got.c3 != PASS


def test_infinite_times_never_pass() -> None:
    runs, chunks, singles = copy.deepcopy(_good())
    _split_row(runs)["split_makespan_ms"] = math.inf
    assert _ev(runs, chunks, singles).c2 != PASS


# ---- the CLI on bad files -------------------------------------------------


def test_report_cli_missing_or_garbled_parquet_is_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "bad.parquet").write_text("garbage")
    (tmp_path / "empty.parquet").write_bytes(b"")
    for runs in ("missing.parquet", "bad.parquet", "empty.parquet"):
        argv = [
            "report",
            str(tmp_path / runs),
            str(tmp_path / runs),
            "--split",
            "j",
            "--single",
            "s",
        ]
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
        except Exception:  # noqa: BLE001, S112 - a crash is not a pass either
            continue
        assert code != 0, runs


def test_report_cli_missing_file_reports_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """G9-D1 (fixed in fix round 1): a missing/garbled export is a one-line
    error with a documented exit code (2 usage / 4 inconclusive), not a
    traceback."""
    argv = ["report", str(tmp_path / "no.parquet"), str(tmp_path / "no2.parquet")]
    argv += ["--split", "j", "--single", "s"]
    code = main(argv)
    assert code in (2, 4)


def test_report_cli_wrong_schema_parquet_is_inconclusive(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    pq.write_table(pa.table({"x": [1, 2]}), tmp_path / "r.parquet")
    pq.write_table(pa.table({"y": [1, 2]}), tmp_path / "c.parquet")
    code = main(
        [
            "report",
            str(tmp_path / "r.parquet"),
            str(tmp_path / "c.parquet"),
            "--split",
            "j",
            "--single",
            "s",
        ]
    )
    assert code == 4
