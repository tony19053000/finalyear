"""V0.9 G0 item 2 (D210 item 4): ``scripts/ui_perf`` is a small package
(clocks, scenarios, measure, events, report, cli) and its CLI entry still
works - as a directory run (the lab checklist's command) and with ``-m``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mf_ui_perf import UI_PERF_DIR, load_ui_perf

pytestmark = pytest.mark.unit

ROOT = UI_PERF_DIR.parents[1]
MODULES = {"clocks", "scenarios", "measure", "events", "report", "cli"}
MAX_MODULE_LINES = 300


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("MF_PASSWORD", "MF_TOKEN")}
    return subprocess.run(
        [sys.executable, *argv],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def test_the_single_file_is_gone_and_the_modules_are_small() -> None:
    assert not (ROOT / "scripts" / "ui_perf.py").exists()
    found = {p.stem for p in UI_PERF_DIR.glob("*.py")} - {"__init__", "__main__"}
    assert found == MODULES
    for path in UI_PERF_DIR.glob("*.py"):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines <= MAX_MODULE_LINES, (path.name, lines)


@pytest.mark.parametrize(
    "entry", [("scripts/ui_perf",), ("-m", "scripts.ui_perf")], ids=["dir", "-m"]
)
def test_the_cli_entry_runs(entry: tuple[str, ...]) -> None:
    helped = _run(*entry, "--help")
    assert helped.returncode == 0, helped.stderr
    assert helped.stdout.startswith("usage: ui_perf"), helped.stdout
    assert "--controller" in helped.stdout
    missing = _run(*entry, "--controller", "https://127.0.0.1:1", "--ca", "ca.crt")
    assert missing.returncode == 2, missing
    assert "set MF_PASSWORD" in missing.stdout


def test_the_package_exports_what_the_tests_and_the_e2e_use() -> None:
    perf = load_ui_perf("ui_perf_v09_g0_package")
    for name in (
        "Api",
        "HeartbeatSamples",
        "JobStateSamples",
        "MetricsFrames",
        "MIXED_CANCELLED",
        "PAGE_PROBES",
        "THRESHOLDS_MS",
        "check",
        "machine",
        "run",
        "seed_bulk",
        "seed_history",
        "summary",
        "table",
        "tiny_project",
        "verdict",
    ):
        assert hasattr(perf, name), name
    assert set(perf.__all__) <= set(dir(perf))


def test_loading_twice_under_one_name_gives_the_same_module() -> None:
    assert load_ui_perf("ui_perf_v09_g0_twice") is load_ui_perf("ui_perf_v09_g0_twice")


def test_the_package_path_is_the_repository_one() -> None:
    assert Path(__file__).resolve().parents[2] / "scripts" / "ui_perf" == UI_PERF_DIR
