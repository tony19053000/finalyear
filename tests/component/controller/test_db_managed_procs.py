"""Spike SP2 (0.17.0): the process mechanics of the managed PostgreSQL, for real.

No PostgreSQL needed - a Python sleeper stands in for the postmaster:

* the parent-death shim (:func:`with_death_signal`): a "controller" process
  spawns the sleeper through the shim and is SIGKILLed; the sleeper must receive
  **SIGINT** (PostgreSQL's fast shutdown) - and without the shim it must survive
  (the orphan case the restart reconciliation exists for);
* :func:`stop_foreign` escalates SIGINT -> SIGQUIT -> SIGKILL on a process that is
  not our child and ignores the gentler signals.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meshforge.controller.db_managed.commands import with_death_signal
from meshforge.controller.db_managed.procs import pid_alive, stop_foreign

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pdeathsig"),
    pytest.mark.timeout(60),
]

#: The stand-in postmaster: records the signal it got, then exits.
_SLEEPER = r"""
import signal, sys, time
out = sys.argv[1]
def got(signum, frame):
    open(out, "w").write(signal.Signals(signum).name)
    sys.exit(0)
for s in (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT):
    signal.signal(s, got)
print("ready", flush=True)
time.sleep(60)
"""

#: The stand-in controller: spawns the sleeper (shimmed or not), prints its pid.
_PARENT = r"""
import json, os, subprocess, sys
argv = json.loads(sys.argv[1])
child = subprocess.Popen(argv, stdout=subprocess.PIPE, start_new_session=True)
child.stdout.readline()
print(json.dumps({"child": child.pid}), flush=True)
import time; time.sleep(60)
"""


def _spawn_parent(sleeper_argv: list[str]) -> tuple[subprocess.Popen[str], int]:
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, json.dumps(sleeper_argv)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    line = parent.stdout.readline()
    return parent, int(json.loads(line)["child"])


def _wait_gone(pid: int, timeout_s: float) -> float | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        if not pid_alive(pid):
            return time.monotonic() - started
        time.sleep(0.01)
    return None


def test_shim_delivers_sigint_when_the_parent_is_killed(tmp_path: Path) -> None:
    record = tmp_path / "signal.txt"
    base = [sys.executable, "-c", _SLEEPER, str(record)]
    # The shim must know the *parent's* pid: build it inside the parent.
    parent_code = _PARENT.replace(
        "argv = json.loads(sys.argv[1])",
        "from meshforge.controller.db_managed.commands import with_death_signal\n"
        "argv = with_death_signal(json.loads(sys.argv[1]), parent_pid=os.getpid())",
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, json.dumps(base)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    child = int(json.loads(parent.stdout.readline())["child"])
    try:
        assert pid_alive(child)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        gone = _wait_gone(child, 5.0)
        assert gone is not None, "the child outlived its SIGKILLed parent"
        assert record.read_text() == "SIGINT"
    finally:
        if pid_alive(child):
            os.kill(child, signal.SIGKILL)
        parent.stdout.close()


def test_without_shim_the_child_is_orphaned(tmp_path: Path) -> None:
    record = tmp_path / "signal.txt"
    parent, child = _spawn_parent([sys.executable, "-c", _SLEEPER, str(record)])
    try:
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        assert _wait_gone(child, 1.0) is None  # survives: the case reconcile handles
        assert not record.exists()
    finally:
        if pid_alive(child):
            os.kill(child, signal.SIGKILL)
            _wait_gone(child, 5.0)
        if parent.stdout is not None:
            parent.stdout.close()


def test_shim_refuses_when_parent_already_gone() -> None:
    # parent_pid that is not our pid: the shim must exit 137 instead of exec'ing.
    argv = with_death_signal(
        [sys.executable, "-c", "print('ran')"], parent_pid=os.getpid() + 1_000_000
    )
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 137
    assert "ran" not in done.stdout


@pytest.mark.asyncio
async def test_stop_foreign_escalates_past_ignored_signals() -> None:
    stubborn = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGQUIT, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n",
        stdout=asyncio.subprocess.PIPE,
    )
    assert stubborn.stdout is not None
    await stubborn.stdout.readline()
    reaper = asyncio.create_task(stubborn.wait())  # we are its parent: reap it
    result = await stop_foreign(
        stubborn.pid,
        steps=(
            (signal.SIGINT, 0.3),
            (signal.SIGQUIT, 0.3),
            (signal.SIGKILL, 5.0),
        ),
    )
    await reaper
    assert result.stopped
    assert result.signal == "SIGKILL"
    assert stubborn.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_stop_foreign_on_a_dead_pid_is_a_no_op() -> None:
    gone = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await gone.wait()
    result = await stop_foreign(gone.pid)
    assert result.stopped
    assert result.signal is None
