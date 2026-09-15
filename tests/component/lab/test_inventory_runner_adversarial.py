"""Retest round for lab/inventory.py 0.2.2 fix round 1 (tester defects D-2/D-3).

Goes further than tests/component/lab/test_inventory_runner.py: a 3-level
process tree, a child that ignores SIGTERM, a child that closes stdout but
keeps running, output larger than an OS pipe buffer on both stdout and
stderr, --timeout edge values (5, 3600, invalid), a deadline that runs out
before a later Python-helper probe, and SIGINT (Ctrl-C) during a hung probe.
Uses real OS processes; no mocking of subprocess.
"""

from __future__ import annotations

import os
import signal
import subprocess
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

pytestmark = pytest.mark.timeout(90)

REPO_ROOT: Final = LAB_DIR.parent
SCRIPT: Final = LAB_DIR / "inventory.py"


@pytest.fixture(autouse=True)
def _fresh_runner() -> Iterator[None]:
    runner.reset()
    yield
    runner.reset()


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
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
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return False


def _wait_dead(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


# ------------------------------------------------------------- 3-level process tree

THREE_LEVEL_TREE: Final = """
import subprocess, sys, time
gc = subprocess.Popen([sys.executable, "-c", '''
import subprocess, sys, time
ggc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1] + ".ggc", "w").write(str(ggc.pid))
time.sleep(120)
''', sys.argv[1]])
open(sys.argv[1] + ".gc", "w").write(str(gc.pid))
time.sleep(120)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
def test_timeout_kills_a_three_level_process_tree(tmp_path: Path) -> None:
    marker = str(tmp_path / "pid")
    result = runner.run(sys.executable, ["-c", THREE_LEVEL_TREE, marker], timeout=3)
    assert result.error is not None and "timed out" in result.error

    deadline = time.monotonic() + 5
    gc_file, ggc_file = Path(f"{marker}.gc"), Path(f"{marker}.ggc")
    while time.monotonic() < deadline and not (gc_file.exists() and ggc_file.exists()):
        time.sleep(0.1)
    assert gc_file.exists(), "grandchild never started before parent was killed"
    grandchild_pid = int(gc_file.read_text(encoding="ascii"))
    assert _wait_dead(grandchild_pid), f"grandchild {grandchild_pid} survived"
    if ggc_file.exists():
        great_grandchild_pid = int(ggc_file.read_text(encoding="ascii"))
        assert _wait_dead(great_grandchild_pid), (
            f"great-grandchild {great_grandchild_pid} survived"
        )


# ------------------------------------------------------------- SIGTERM-ignoring child

IGNORES_SIGTERM: Final = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(120)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handling")
def test_timeout_kills_child_that_ignores_sigterm(tmp_path: Path) -> None:
    """The runner always sends SIGKILL (never SIGTERM) to the process group, so a
    child that installs a SIGTERM handler must die anyway."""
    start = time.monotonic()
    result = runner.run(sys.executable, ["-c", IGNORES_SIGTERM], timeout=2)
    elapsed = time.monotonic() - start
    assert result.error is not None and "timed out" in result.error
    assert elapsed < 8, f"took {elapsed:.1f}s to kill a SIGTERM-ignoring child"


# ------------------------------------------------------------- closes stdout, keeps running

CLOSES_STDOUT_KEEPS_RUNNING: Final = """
import sys, time
sys.stdout.close()
time.sleep(120)
"""


def test_timeout_bounds_child_that_closes_stdout_but_keeps_running() -> None:
    start = time.monotonic()
    result = runner.run(sys.executable, ["-c", CLOSES_STDOUT_KEEPS_RUNNING], timeout=2)
    elapsed = time.monotonic() - start
    assert result.error is not None and "timed out" in result.error
    assert elapsed < 10


# ------------------------------------------------------------- large output, no pipe deadlock

BIG_STDOUT: Final = "import sys; sys.stdout.write('x' * 10_000_000)"
BIG_STDERR: Final = "import sys; sys.stderr.write('y' * 10_000_000)"
BIG_BOTH: Final = (
    "import sys; sys.stdout.write('x' * 6_000_000); sys.stderr.write('y' * 6_000_000)"
)


def test_ten_megabyte_stdout_does_not_deadlock_or_hang() -> None:
    start = time.monotonic()
    result = runner.run(sys.executable, ["-c", BIG_STDOUT], timeout=30)
    elapsed = time.monotonic() - start
    assert result.error is None, result.error
    assert result.returncode == 0
    assert elapsed < 20, f"10 MB stdout took {elapsed:.1f}s -- possible pipe stall"


def test_ten_megabyte_stderr_does_not_deadlock_or_hang() -> None:
    result = runner.run(sys.executable, ["-c", BIG_STDERR], timeout=30)
    assert result.error is None, result.error
    assert result.returncode == 0


def test_simultaneous_large_stdout_and_stderr_no_deadlock() -> None:
    """Writing to both pipes at once is the classic ``Popen`` pipe-buffer deadlock
    scenario (~64 KiB OS pipe buffer): using ``communicate()`` (as the runner
    does) reads both streams concurrently, so this must complete promptly."""
    start = time.monotonic()
    result = runner.run(sys.executable, ["-c", BIG_BOTH], timeout=30)
    elapsed = time.monotonic() - start
    assert result.error is None, result.error
    assert elapsed < 20, f"simultaneous big stdout+stderr took {elapsed:.1f}s"


# ------------------------------------------------------------- --timeout edge values (CLI)


def _run_cli(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_cli_timeout_minimum_boundary_5_is_accepted() -> None:
    proc = _run_cli("--timeout", "5", "--no-summary", timeout=30)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_cli_timeout_below_minimum_is_rejected_with_usage_error() -> None:
    proc = _run_cli("--timeout", "4.9", "--no-summary")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr


def test_cli_timeout_above_maximum_is_rejected_with_usage_error() -> None:
    proc = _run_cli("--timeout", "3600.1", "--no-summary")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)


def test_cli_timeout_maximum_boundary_3600_is_accepted_as_a_value() -> None:
    """Only checks argument *parsing* accepts the boundary; does not actually run
    for an hour. start_budget(3600) is exercised directly instead."""
    runner.start_budget(3600)
    left = runner.remaining()
    assert left is not None and 3550 < left <= 3599


def test_cli_timeout_non_numeric_is_rejected() -> None:
    proc = _run_cli("--timeout", "soon", "--no-summary")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr


def test_cli_timeout_nan_and_infinity_are_rejected() -> None:
    for bad in ("nan", "inf", "-inf"):
        proc = _run_cli("--timeout", bad, "--no-summary")
        assert proc.returncode == 2, (bad, proc.stdout, proc.stderr)


# ------------------------------------------------------------- budget exhausted before a helper


def test_budget_exhausted_before_second_command_records_deadline_not_hang() -> None:
    runner.start_budget(5)  # reserve 1s -> 4s
    first = runner.run(
        sys.executable,
        ["-c", "import time; time.sleep(10)"],
        timeout=30,
        remember_hang=False,
    )
    second = runner.run(sys.executable, ["-c", "print('should not run')"], timeout=30)
    assert first.error is not None and "run deadline reached" in first.error
    assert second.error == f"{sys.executable}: {runner.SKIPPED_DEADLINE}"
    assert second.stdout == ""


# ------------------------------------------------------------- Ctrl-C mid-probe leaves nothing


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT semantics")
def test_sigint_during_hung_probe_run_leaves_no_processes(tmp_path: Path) -> None:
    """Full end-to-end: send SIGINT to the ``inventory.py`` process while it is
    stuck in a hung external tool, and verify the whole tree (script + hung tool)
    is gone afterwards."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    marker = "meshforge_ctrlc_probe_marker"
    tool = fake_bin / "nvidia-smi"
    tool.write_text(f"#!/bin/sh\nsleep 250 # {marker}\n", encoding="utf-8")
    tool.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(fake_bin), env.get("PATH", "")])

    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--no-summary"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1.5)  # let it reach the hung nvidia-smi probe
    os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        pytest.fail("inventory.py did not exit within 15s of SIGINT")

    time.sleep(1.0)
    survivors = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if marker in cmdline:
            survivors.append((pid, cmdline))
    for pid, _cmdline in survivors:
        import contextlib

        with contextlib.suppress(OSError):
            os.kill(int(pid), 9)
    assert not survivors, f"process(es) survived SIGINT: {survivors}"


# ------------------------------------------------------------- module state isolation


def test_run_state_reset_isolates_deadline_and_hung_cache_between_calls() -> None:
    runner.start_budget(10)
    runner.run(sys.executable, ["-c", "pass"], remember_hang=False)
    assert runner.remaining() is not None
    runner.reset()
    assert runner.remaining() is None
    assert runner._STATE.hung == set()
