"""V0.15 G3 step 0 (G2 tech debt): ``compute planner ml compare --timeout S``.

A **real** child process that never finishes (a stand-in module on
``PYTHONPATH`` that records its pid and sleeps) is stopped after ``--timeout``:
exit 1, a one-line message, and the child is gone (killed, not orphaned).
The default is one hour and is passed to ``subprocess.run``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from meshforge.cli import planner_commands as pc
from meshforge.cli.app import app

pytestmark = pytest.mark.unit

_SLEEPER = """
import os, sys, time
from pathlib import Path
Path(os.environ["MF_SLEEPER_PID"]).write_text(str(os.getpid()))
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) would *terminate* the process on Windows; ask tasklist.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        ).stdout
        return f" {pid} " in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); subprocess.run reaps it after kill()
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split()[2] == "Z")


@pytest.mark.timeout(60)
def test_a_child_that_overruns_is_killed_and_the_cli_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "mf_sleeper_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(_SLEEPER)
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("MF_SLEEPER_PID", str(pid_file))
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.setattr(pc, "_TRAIN_MODULE", "mf_sleeper_pkg")
    monkeypatch.setattr(pc, "ml_missing_libraries", list)
    dataset = tmp_path / "d.parquet"
    dataset.write_bytes(b"PAR1")
    started = time.monotonic()
    result = CliRunner().invoke(
        app,
        ["planner", "ml", "compare", "--dataset", str(dataset), "--timeout", "2"],
    )
    took = time.monotonic() - started
    assert result.exit_code == 1, result.output
    assert "did not finish in 2 s and was stopped" in result.output
    assert took < 30
    pid = int(pid_file.read_text())
    assert not _alive(pid)


def test_the_default_timeout_is_one_hour_and_reaches_subprocess_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    monkeypatch.setattr("meshforge.cli.planner_commands.subprocess.run", fake)
    monkeypatch.setattr(pc, "ml_missing_libraries", list)
    dataset = tmp_path / "d.parquet"
    dataset.write_bytes(b"PAR1")
    result = CliRunner().invoke(
        app, ["planner", "ml", "compare", "--dataset", str(dataset)]
    )
    assert result.exit_code == 1
    assert seen["timeout"] == 3600.0


def test_a_timeout_below_one_second_is_a_usage_error(tmp_path: Path) -> None:
    dataset = tmp_path / "d.parquet"
    dataset.write_bytes(b"PAR1")
    result = CliRunner().invoke(
        app,
        ["planner", "ml", "compare", "--dataset", str(dataset), "--timeout", "0"],
    )
    assert result.exit_code == 2
