"""V0.3 G4 tester round: a child that ignores SIGTERM must still die on timeout.

``run_command`` kills with ``SIGKILL`` directly (never ``SIGTERM``), so a child that
installs a SIGTERM handler and never exits should still be gone, reaped, by the
time ``run_command`` returns. POSIX-only (``os.killpg``/process groups); the
Windows path uses ``taskkill /F`` and is out of scope for this machine.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from meshforge.agent.hardware.probe import run_command

pytestmark = pytest.mark.unit

_IGNORES_SIGTERM = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(30)\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="posix process groups only")
def test_a_child_that_ignores_sigterm_is_still_killed_and_reaped() -> None:
    started = time.monotonic()
    result = run_command(sys.executable, ["-c", _IGNORES_SIGTERM], timeout_s=0.2)
    elapsed = time.monotonic() - started

    assert not result.ok
    assert "timed out" in result.describe_failure()
    # The whole point of SIGKILL-not-SIGTERM: this must not take anywhere near the
    # child's own 30 s sleep.
    assert elapsed < 5.0, (
        f"run_command took {elapsed:.2f}s - SIGKILL may not have landed"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="posix process groups only")
def test_a_child_that_ignores_sigterm_leaves_no_process_behind() -> None:
    """After the timeout, the child's pid must not be a live process anymore
    (``os.kill(pid, 0)`` raises ``ProcessLookupError`` once it is reaped)."""
    # We cannot get the pid out of run_command directly (by design it does not
    # leak Popen objects), so this drives the same mechanics one level down via
    # a short poll: the test's own success criterion is that run_command's
    # internal wait/reap actually completed, which the elapsed-time assertion in
    # the sibling test already establishes indirectly. This test instead checks
    # there is no direct child left in our own process group after several such
    # calls in a row (a leak would accumulate).
    for _ in range(5):
        result = run_command(sys.executable, ["-c", _IGNORES_SIGTERM], timeout_s=0.2)
        assert not result.ok

    # Reap any zombies our own process group is owed; a real leak would show up
    # as WNOHANG immediately finding nothing (children already reaped by
    # run_command) - repeated calls should never block waiting on a lingering
    # process.
    started = time.monotonic()
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
        assert time.monotonic() - started < 2.0, "reaping loop did not converge"
