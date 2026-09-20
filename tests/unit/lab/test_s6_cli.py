"""Unit tests for the s6_environments.py command line (0.2.10): parsing,
usage errors, exit codes, and the report command on prepared folders.
Nothing is measured here (``run_environments`` is replaced by a stub)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from spikes._harness.envelope import make_envelope
from spikes._harness.reps import CaseResult
from spikes._s6.run import EnvironmentRun, RunConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "lab" / "spikes" / "s6_environments.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("s6_environments", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["s6_environments"] = module
    spec.loader.exec_module(module)
    return module


s6 = _load()


def _parse(argv: list[str]) -> Any:
    return s6._parser().parse_args(argv)


def test_run_defaults() -> None:
    args = _parse(["run"])
    assert args.command == "run"
    assert (args.reps, args.warmup, args.quick, args.timeout) == (0, -1, False, 0.0)
    assert not (
        args.skip_docker or args.skip_gpu or args.skip_gpu_torch or args.no_network
    )
    assert args.gpu_torch_image == "pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime"
    assert args.name is None and args.out_dir is None
    assert not hasattr(args, "link")


def test_run_flags() -> None:
    args = _parse(
        [
            "run",
            "--quick",
            "--reps",
            "7",
            "--warmup",
            "0",
            "--timeout",
            "60",
            "--skip-docker",
            "--skip-gpu",
            "--skip-gpu-torch",
            "--no-network",
            "--gpu-torch-image",
            "x/y:1.0",
            "--name",
            "L2",
            "--out-dir",
            "somewhere",
        ]
    )
    assert args.quick and args.reps == 7 and args.warmup == 0 and args.timeout == 60.0
    assert (
        args.skip_docker and args.skip_gpu and args.skip_gpu_torch and args.no_network
    )
    assert args.gpu_torch_image == "x/y:1.0" and args.name == "L2"
    assert args.out_dir == Path("somewhere")


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--reps", "0"],
        ["run", "--reps", "x"],
        ["run", "--timeout", "1"],
        ["run", "--timeout", "999999"],
        ["run", "--pause", "-1"],
        ["run", "--name", "has spaces"],
        ["run", "--name", "x" * 65],
        ["run", "--gpu-torch-image", "--rm"],
        ["run", "--gpu-torch-image", "bad image"],
        ["run", "--link", "wifi"],
        ["report", "--out", "x.md"],
        ["report", "--results", "dir"],
        ["report", "--results", "d", "--out", "o", "--labels", "nolabel"],
        ["report", "--results", "d", "--out", "o", "--date", "yesterday"],
        [],
        ["nonsense"],
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(argv)
    assert exc.value.code == s6.EXIT_USAGE


def test_labels_parsing() -> None:
    assert s6._labels("L1=hostA, L2=hostB,") == {"hostA": "L1", "hostB": "L2"}


def _envelope(*, complete_cases: bool = True, host: str = "lab1") -> dict[str, Any]:
    case = CaseResult(
        case_id="docker-start", label="x", metrics={"start_to_exit_ms": "ms"}
    )
    case.samples = [{"start_to_exit_ms": 200.0}] * (5 if complete_cases else 2)
    return make_envelope(
        spike="S6",
        topic="S6-environments",
        link=None,
        params={},
        cases=[case],
        extra={
            "docker": {"ok": True},
            "facts": {"gpu": {"present": False}},
            "warnings": [],
        },
        git={"commit": "abc", "dirty": False},
        host={"hostname": host},
    )


def _stub_run(monkeypatch: pytest.MonkeyPatch, *, complete: bool) -> list[RunConfig]:
    seen: list[RunConfig] = []

    def fake(cfg: RunConfig, *, log: Any) -> EnvironmentRun:
        seen.append(cfg)
        return EnvironmentRun(
            envelope=_envelope(complete_cases=complete), complete=complete, warnings=[]
        )

    monkeypatch.setattr(s6, "run_environments", fake)
    return seen


def test_cmd_run_writes_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _stub_run(monkeypatch, complete=True)
    code = s6.main(["run", "--out-dir", str(tmp_path), "--quick", "--skip-gpu"])
    assert code == s6.EXIT_OK
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["spike"] == "S6"
    assert seen[0].quick and seen[0].skip_gpu and seen[0].effective_reps() == 2
    err = capsys.readouterr().err
    assert "QUICK MODE" in err and "every row will be INVALID" in err
    assert (
        "docker: usable" in err and "runtime recommendation (ADR-0004): docker" in err
    )


def test_cmd_run_incomplete_exits_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_run(monkeypatch, complete=False)
    assert s6.main(["run", "--out-dir", str(tmp_path)]) == s6.EXIT_INCOMPLETE


def test_cmd_run_collision_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_run(monkeypatch, complete=True)
    assert s6.main(["run", "--out-dir", str(tmp_path)]) == s6.EXIT_OK
    path = next(tmp_path.glob("*.json"))
    other = _envelope(host="someone-else")
    path.write_text(json.dumps(other), encoding="utf-8")
    assert s6.main(["run", "--out-dir", str(tmp_path)]) == s6.EXIT_IO


def test_cmd_run_unwritable_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_run(monkeypatch, complete=True)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert s6.main(["run", "--out-dir", str(blocker / "sub")]) == s6.EXIT_IO


def test_cmd_run_ctrl_c_exits_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(cfg: RunConfig, *, log: Any) -> EnvironmentRun:
        raise KeyboardInterrupt

    monkeypatch.setattr(s6, "run_environments", boom)
    assert s6.main(["run", "--out-dir", str(tmp_path)]) == s6.EXIT_INTERRUPTED
    assert not list(tmp_path.glob("*.json"))


def test_cmd_report_from_folder(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-20-S6-environments"
    results.mkdir()
    (results / "lab1.json").write_text(json.dumps(_envelope()), encoding="utf-8")
    (results / "broken.json").write_text("{not json", encoding="utf-8")
    (results / "other.json").write_text(
        json.dumps({**_envelope(), "spike": "S7"}), encoding="utf-8"
    )
    out = tmp_path / "out" / "report.md"
    code = s6.main(
        [
            "report",
            "--results",
            str(results),
            "--out",
            str(out),
            "--labels",
            "L1=lab1,L2=lab2",
        ]
    )
    assert code == s6.EXIT_OK
    text = out.read_text(encoding="utf-8")
    assert "# S6 execution environments — 2026-09-20" in text
    assert "Roadmap task: 0.2.10" in text
    assert "| L1 |" in text and "| L2 | ? | TBD — no result file |" in text
    assert "broken.json" in text and "Skipped result files" in text
    assert "other.json" not in text.split("Skipped result files")[1]


def test_cmd_report_missing_folder_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        s6.main(
            [
                "report",
                "--results",
                str(tmp_path / "nope"),
                "--out",
                str(tmp_path / "o.md"),
            ]
        )
    assert exc.value.code == s6.EXIT_USAGE


def test_cmd_report_empty_folder_is_all_tbd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "r.md"
    assert (
        s6.main(
            [
                "report",
                "--results",
                str(tmp_path),
                "--out",
                str(out),
                "--date",
                "2026-01-02",
            ]
        )
        == 0
    )
    assert "no S6 result files" in capsys.readouterr().err
    assert "— 2026-01-02" in out.read_text(encoding="utf-8")


def test_cmd_report_bad_template_exits_1(tmp_path: Path) -> None:
    template = tmp_path / "t.md"
    template.write_text("no placeholders", encoding="utf-8")
    code = s6.main(
        [
            "report",
            "--results",
            str(tmp_path),
            "--out",
            str(tmp_path / "o.md"),
            "--template",
            str(template),
        ]
    )
    assert code == s6.EXIT_IO
