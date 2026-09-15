"""lab/_inventory/runner.py against real OS processes (0.2.2 fix round 1).

A timed-out command must die with its whole process tree, be reaped, and the run
budget must bound total time. Uses real ``python`` child processes (portable), no
mocks of subprocess.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

LAB_DIR: Final = Path(__file__).resolve().parents[3] / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from _inventory import runner  # noqa: E402

pytestmark = pytest.mark.timeout(60)

#: Parent starts a grandchild that writes its pid, then both sleep.
PARENT_WITH_GRANDCHILD: Final = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


@pytest.fixture(autouse=True)
def _fresh_runner() -> Iterator[None]:
    runner.reset()
    yield
    runner.reset()


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for our purposes only if reaped
    # by someone. Our grandchild's parent is killed too, so init reaps it.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return False


def test_timeout_kills_grandchildren(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    start = time.monotonic()
    result = runner.run(
        sys.executable, ["-c", PARENT_WITH_GRANDCHILD, str(pid_file)], timeout=3
    )
    assert time.monotonic() - start < 15
    assert result.error == f"{sys.executable}: timed out after 3 s"
    grandchild = int(pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), f"grandchild {grandchild} survived the timeout"


def test_budget_bounds_total_time_and_skips_the_rest() -> None:
    runner.start_budget(6)  # reserve 1 s -> 5 s for commands
    start = time.monotonic()
    sleeper = ["-c", "import time; time.sleep(60)"]
    first = runner.run(sys.executable, sleeper, timeout=30, remember_hang=False)
    second = runner.run(sys.executable, sleeper, timeout=30, remember_hang=False)
    elapsed = time.monotonic() - start
    assert first.error is not None and "run deadline reached" in first.error
    assert second.error == f"{sys.executable}: {runner.SKIPPED_DEADLINE}"
    assert elapsed < 10


def test_normal_command_output_and_exit_code() -> None:
    result = runner.run(
        sys.executable, ["-c", "import sys; print('hi ü'); sys.exit(4)"], timeout=30
    )
    assert (result.returncode, result.stdout.strip(), result.ok) == (4, "hi ü", False)
