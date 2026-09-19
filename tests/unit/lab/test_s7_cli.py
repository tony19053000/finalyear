"""Unit tests for the s7_hardware.py command line (0.2.11): parsing and summary.

Nothing is measured here; the real run is a component test.
"""

from __future__ import annotations

import importlib.util
import json
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
    assert not hasattr(args, "link")  # S7 has no --link: the envelope gets null


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
        ["run", "--link", "ethernet"],  # S7 has no link: not accepted
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
    args = _parse(["report", "--results", "a", "--results", "b", "--out", "o.md"])
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


def test_summary_of_a_case_without_any_metric(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = _envelope()
    doc["cases"] = {"x": {"label": "x", "metrics": {}, "samples": [], "summary": {}}}
    s7.print_summary(doc, reps=5)
    assert "—" in capsys.readouterr().err


# ------------------------------------------------------------------ cmd_run
def _fake_run(
    monkeypatch: pytest.MonkeyPatch, *, complete: bool, hostname: str = "hostA"
) -> list[Any]:
    """Replace the measurement with a canned envelope; returns the configs seen."""
    from spikes._harness.envelope import make_envelope
    from spikes._s7.run import HardwareRun

    seen: list[Any] = []

    def run_scores(cfg: Any, *, log: Any) -> HardwareRun:
        seen.append(cfg)
        log("fake measurement")
        doc = make_envelope(
            spike="S7",
            topic="S7-hardware",
            link=cfg.link,
            host_label=cfg.name,
            params={},
            cases=[],
            errors=[] if complete else [{"failure_type": "x", "message": "y"}],
            extra={"warnings": []},
        )
        doc["host"]["hostname"] = hostname
        return HardwareRun(envelope=doc, complete=complete, warnings=[])

    monkeypatch.setattr(s7, "run_scores", run_scores)
    return seen


def test_run_writes_the_envelope_with_link_null_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _fake_run(monkeypatch, complete=True)
    code = s7.main(["run", "--quick", "--out-dir", str(tmp_path), "--name", "L1"])
    assert code == s7.EXIT_OK
    assert seen[0].link is None  # S7 sends no traffic: never fakes a link
    assert seen[0].profile.quick is True
    assert seen[0].name == "L1"
    files = sorted(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert files[0].name.startswith("l1")  # harness: case-folded safe file name
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["link"] is None
    assert doc["host"]["label"] == "L1"
    err = capsys.readouterr().err
    assert "QUICK MODE" in err
    assert "written:" in err


def test_run_exits_3_when_the_result_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, complete=False)
    assert s7.main(["run", "--out-dir", str(tmp_path)]) == s7.EXIT_INCOMPLETE
    assert list(tmp_path.glob("*.json"))  # the partial result is still written


def test_run_warns_when_reps_is_below_the_validity_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _fake_run(monkeypatch, complete=True)
    s7.main(["run", "--reps", "2", "--warmup", "0", "--out-dir", str(tmp_path)])
    assert "--reps 2 < 5: every score will be INVALID" in capsys.readouterr().err
    assert (seen[0].effective_reps(), seen[0].effective_warmup()) == (2, 0)


def test_run_passes_every_flag_into_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_run(monkeypatch, complete=True)
    s7.main(
        [
            "run",
            "--out-dir",
            str(tmp_path),
            "--timeout",
            "42",
            "--pause",
            "0",
            "--skip-gpu",
            "--device",
            "1",
        ]
    )
    cfg = seen[0]
    assert (cfg.effective_budget(), cfg.pause_s) == (42.0, 0.0)
    assert (cfg.skip_gpu, cfg.device_index) == (True, 1)
    assert cfg.profile.quick is False


def test_run_exits_1_when_the_output_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_run(monkeypatch, complete=True)
    blocker = tmp_path / "file-not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    code = s7.main(["run", "--out-dir", str(blocker)])
    assert code == s7.EXIT_IO
    assert "cannot write" in capsys.readouterr().err


def test_run_refuses_to_overwrite_another_hosts_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_run(monkeypatch, complete=True, hostname="hostA")
    assert s7.main(["run", "--out-dir", str(tmp_path), "--name", "l1"]) == s7.EXIT_OK
    _fake_run(monkeypatch, complete=True, hostname="hostB")
    code = s7.main(["run", "--out-dir", str(tmp_path), "--name", "l1"])
    assert code == s7.EXIT_IO
    assert "already holds results from host 'hostA'" in capsys.readouterr().err
    doc = json.loads((tmp_path / "l1.json").read_text(encoding="utf-8"))
    assert doc["host"]["hostname"] == "hostA"  # untouched


def test_ctrl_c_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(cfg: Any, *, log: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(s7, "run_scores", interrupted)
    assert s7.main(["run", "--out-dir", str(tmp_path)]) == s7.EXIT_INTERRUPTED
    assert "interrupted" in capsys.readouterr().err


def test_report_end_to_end_from_one_result_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_run(monkeypatch, complete=True, hostname="hostA")
    raw = tmp_path / "2026-09-20-S7-hardware"
    s7.main(["run", "--out-dir", str(raw)])
    out = tmp_path / "r.md"
    code = s7.main(
        ["report", "--results", str(raw), "--out", str(out), "--labels", "L1=hostA"]
    )
    assert code == s7.EXIT_OK
    text = out.read_text(encoding="utf-8")
    assert "# S7 hardware scores — 2026-09-20" in text
    assert "| Case | L1 |" in text or "No S7 result files found" not in text
    assert "report written" in capsys.readouterr().err


def test_report_ignores_result_files_of_other_spikes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from spikes._harness.envelope import make_envelope, write_envelope

    raw = tmp_path / "raw"
    doc = make_envelope(
        spike="S1", topic="S1-network", link="wifi", params={}, cases=[], errors=[]
    )
    write_envelope(doc, raw / "s1.json")
    out = tmp_path / "r.md"
    assert s7.main(["report", "--results", str(raw), "--out", str(out)]) == s7.EXIT_OK
    assert "no S7 result files found" in capsys.readouterr().err
