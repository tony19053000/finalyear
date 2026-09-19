"""Unit tests for the s7_hardware.py command line (0.2.11): parsing and summary.

Nothing is measured here; the real run is a component test.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "lab" / "spikes" / "s7_hardware.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("s7_hardware", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["s7_hardware"] = module
    spec.loader.exec_module(module)
    return module


s7 = _load()


def _parse(argv: list[str]) -> Any:
    return s7._parser().parse_args(argv)


def test_run_defaults() -> None:
    args = _parse(["run"])
    assert args.command == "run"
    assert args.quick is False
    assert args.reps == 0  # 0 = take the profile default
    assert args.warmup == -1
    assert args.timeout == 0.0
    assert args.skip_gpu is False
    assert args.device == 0
    assert args.link == "ethernet"


def test_quick_flag_and_overrides() -> None:
    args = _parse(["run", "--quick", "--reps", "7", "--warmup", "0", "--timeout", "60"])
    assert (args.quick, args.reps, args.warmup, args.timeout) == (True, 7, 0, 60.0)


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--reps", "0"],
        ["run", "--reps", "notanumber"],
        ["run", "--timeout", "1"],  # below MIN_BUDGET_S
        ["run", "--timeout", "999999"],
        ["run", "--device", "99"],
        ["run", "--pause", "-1"],
        ["run", "--link", "carrier-pigeon"],
        ["run", "--name", "has spaces"],
        ["run", "--name", "way-too-long" * 10],
        ["report", "--out", "x.md"],  # --results is required
        ["report", "--results", "dir"],  # --out is required
        ["report", "--results", "d", "--out", "o", "--labels", "brokenlabel"],
        ["report", "--results", "d", "--out", "o", "--date", "not-a-date"],
        [],  # no subcommand
        ["nonsense"],
    ],
)
def test_usage_errors_exit_with_code_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(argv)
    assert exc.value.code == s7.EXIT_USAGE


def test_labels_are_parsed_host_to_label() -> None:
    args = _parse(
        ["report", "--results", "d", "--out", "o", "--labels", "L1=hostA, L2=hostB"]
    )
    assert args.labels == {"hostA": "L1", "hostB": "L2"}


def test_results_can_be_repeated() -> None:
    args = _parse(
        ["report", "--results", "a", "--results", "b", "--out", "o.md"]
    )
    assert [p.name for p in args.results] == ["a", "b"]


def test_report_day_comes_from_the_folder_name_then_the_flag() -> None:
    dirs = [Path("lab/reports/raw/2026-09-20-S7-hardware")]
    assert s7._report_day(dirs, None) == date(2026, 9, 20)
    assert s7._report_day(dirs, date(2020, 1, 2)) == date(2020, 1, 2)
    assert s7._report_day([Path("whatever")], None) is not None


def test_report_with_a_missing_folder_is_a_usage_error(tmp_path: Path) -> None:
    parser = s7._parser()
    args = parser.parse_args(
        ["report", "--results", str(tmp_path / "nope"), "--out", str(tmp_path / "o.md")]
    )
    with pytest.raises(SystemExit) as exc:
        s7.cmd_report(args, parser)
    assert exc.value.code == s7.EXIT_USAGE


def test_exit_codes_are_the_documented_ones() -> None:
    assert (s7.EXIT_OK, s7.EXIT_IO, s7.EXIT_USAGE) == (0, 1, 2)
    assert (s7.EXIT_INCOMPLETE, s7.EXIT_INTERRUPTED) == (3, 130)


def _envelope() -> dict[str, Any]:
    return {
        "host": {"label": "L1", "hostname": "hostA"},
        "cases": {
            "cpu-float32-all": {
                "label": "CPU",
                "metrics": {"gflops": "GFLOP/s", "elapsed_s": "s"},
                "samples": [{}] * 5,
                "summary": {
                    "gflops": {"mean": 123.456, "stdev": 1.2},
                    "elapsed_s": {"mean": 0.3, "stdev": 0.01},
                },
            },
            "gpu-float32": {
                "label": "GPU",
                "metrics": {"tflops": "TFLOP/s"},
                "samples": [{}] * 2,
                "summary": {"tflops": {"mean": 8.0, "stdev": None}},
            },
        },
        "errors": [{"failure_type": "cuda_unavailable", "message": "no device"}],
        "extra": {"warnings": ["ON BATTERY: ..."]},
    }


def test_summary_marks_invalid_rows_hides_durations_and_shouts_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    s7.print_summary(_envelope(), reps=5)
    err = capsys.readouterr().err
    assert "gflops 123.46 ± 1.20 GFLOP/s" in err
    assert "elapsed_s" not in err
    assert "INVALID (n=2)" in err
    assert "tflops 8.00 ± —" in err
    assert "not measured: cuda_unavailable: no device" in err
    assert "!! ON BATTERY" in err


def test_summary_of_a_case_without_any_metric(capsys: pytest.CaptureFixture[str]) -> None:
    doc = _envelope()
    doc["cases"] = {"x": {"label": "x", "metrics": {}, "samples": [], "summary": {}}}
    s7.print_summary(doc, reps=5)
    assert "—" in capsys.readouterr().err
