"""V0.17 G2 item 7: the shared parent-death shim (common/death_signal.py)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from meshforge.agent.process import _PDEATHSIG_SHIM
from meshforge.common.death_signal import (
    ARM_SNIPPET,
    EXIT_PARENT_GONE,
    SIMPLE_SHIM,
    death_signal_supported,
    with_death_signal,
)
from meshforge.controller.db_managed.commands import (
    DEATH_SIGNAL,
)
from meshforge.controller.db_managed.commands import (
    with_death_signal as pg_with_death_signal,
)

pytestmark = pytest.mark.unit


def test_shims_compile_and_share_the_arming_code() -> None:
    compile(SIMPLE_SHIM, "<simple>", "exec")
    compile(_PDEATHSIG_SHIM, "<agent>", "exec")
    assert ARM_SNIPPET in SIMPLE_SHIM
    assert ARM_SNIPPET in _PDEATHSIG_SHIM  # agent and controller: one copy
    assert "sig = int(signal.SIGKILL)" in _PDEATHSIG_SHIM


def test_wrap_on_linux_only() -> None:
    argv = ["/usr/bin/true", "x"]
    wrapped = with_death_signal(argv, parent_pid=42, signum=2, platform="linux")
    assert wrapped[1:4] == ["-I", "-S", "-c"]
    assert wrapped[5:] == ["42", "2", "/usr/bin/true", "/usr/bin/true", "x"]
    assert with_death_signal(argv, parent_pid=1, signum=2, platform="win32") == argv
    assert death_signal_supported("linux") and not death_signal_supported("darwin")


def test_controller_wrapper_defaults_to_sigint() -> None:
    assert DEATH_SIGNAL == signal.SIGINT
    wrapped = pg_with_death_signal(["/x/postgres"], parent_pid=7, platform="linux")
    assert wrapped[5:7] == ["7", str(int(signal.SIGINT))]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux prctl")
def test_shim_runs_program_and_refuses_dead_parent() -> None:
    ok = subprocess.run(
        with_death_signal(
            ["/bin/sh", "-c", "exit 3"], parent_pid=os.getpid(), signum=2
        ),
        check=False,
    )
    assert ok.returncode == 3
    gone = subprocess.run(
        with_death_signal(["/bin/sh", "-c", "exit 0"], parent_pid=1, signum=2),
        check=False,
    )
    assert gone.returncode == EXIT_PARENT_GONE
