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

#: Parent starts a grandchild, records its pid and (Linux) the kernel's start
#: time of that process (/proc/<pid>/stat field 22), then both sleep. The start
#: time identifies the process: a pid the kernel hands out again later has
#: another one (V0.7 G6 gate hardening).
PARENT_WITH_GRANDCHILD: Final = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
try:
    with open(f"/proc/{child.pid}/stat", encoding="ascii") as f:
        started = f.read().rsplit(") ", 1)[1].split()[19]
except (OSError, IndexError):
    started = "-"
open(sys.argv[1], "w").write(f"{child.pid} {started}")
time.sleep(120)
"""
GONE_WAIT_S: Final = 20.0
"""How long a SIGKILLed tree may take to disappear. The kill itself cannot
miss (the runner kills the whole process group); only the exit of a killed
process can be slow on a machine under heavy I/O - 5 s was exceeded once at
the end of a 60-minute component run (V0.7 G6 hand-off; never reproduced in
120 cycles under 40 CPU burners)."""


@pytest.fixture(autouse=True)
def _fresh_runner() -> Iterator[None]:
    runner.reset()
    yield
    runner.reset()


def _alive(pid: int, started: str = "-") -> bool:
    """``pid`` still runs - and, where ``started`` is known, is still the
    process that was started then (a recycled pid is another process: the
    one we look for is gone)."""
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
            fields = f.read().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return False
    if started != "-" and len(fields) > 19 and fields[19] != started:
        return False  # the pid was handed out again: not our grandchild
    return fields[0] != "Z"


def test_timeout_kills_grandchildren(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    start = time.monotonic()
    result = runner.run(
        sys.executable, ["-c", PARENT_WITH_GRANDCHILD, str(pid_file)], timeout=3
    )
    assert time.monotonic() - start < 15
    assert result.error == f"{sys.executable}: timed out after 3 s"
    pid_text, started = pid_file.read_text(encoding="ascii").split()
    grandchild = int(pid_text)
    deadline = time.monotonic() + GONE_WAIT_S
    while _alive(grandchild, started) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild, started), (
        f"grandchild {grandchild} (started {started}) survived the timeout "
        f"for {GONE_WAIT_S:g} s"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="/proc start time is Linux")
def test_alive_tells_a_recycled_pid_from_the_original() -> None:
    """The hardening itself: this process is alive under its own start time,
    and "not alive" under another one (what a recycled pid looks like)."""
    with open(f"/proc/{os.getpid()}/stat", encoding="ascii") as f:
        mine = f.read().rsplit(") ", 1)[1].split()[19]
    assert _alive(os.getpid(), mine)
    assert _alive(os.getpid())  # start time unknown: the old check
    assert not _alive(os.getpid(), str(int(mine) + 1))


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
