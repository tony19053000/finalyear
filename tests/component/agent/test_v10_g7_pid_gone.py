"""V1.0 G7: ``mf_procs.pid_gone`` never says "alive" again once it said "gone".

The old zombie-or-missing check flipped gone -> alive -> gone for one killed
process (psutil's ``STATUS_DEAD`` while the reaper collects it), which made
``test_grandchildren_are_swept_even_after_the_child_exits_by_itself`` fail with
``assert False`` right after its wait loop had seen the pid gone. Real processes,
killed many times and polled as fast as possible, the way that test does it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

psutil = pytest.importorskip("psutil", reason="needs the agent extra")

from mf_procs import GONE_STATUSES, pid_gone  # noqa: E402 - needs psutil

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX reaping states"),
]

KILLS = 60
POLLS = 200


def test_dead_counts_as_gone() -> None:
    assert psutil.STATUS_DEAD in GONE_STATUSES
    assert psutil.STATUS_ZOMBIE in GONE_STATUSES
    assert psutil.STATUS_RUNNING not in GONE_STATUSES


def test_an_orphan_killed_once_never_reads_alive_after_gone() -> None:
    flips: list[str] = []
    for _ in range(KILLS):
        # a child that starts a grandchild and exits: the grandchild is reaped by
        # init/the subreaper, not by us (the window the flake needed)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "k = subprocess.Popen(['sleep', '600']); "
                "print(k.pid, flush=True)",
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        assert child.stdout is not None
        pid = int(child.stdout.readline())
        child.stdout.close()
        child.wait(timeout=30)
        assert not pid_gone(pid)
        os.killpg(child.pid, signal.SIGKILL)
        seen = "".join("G" if pid_gone(pid) else "a" for _ in range(POLLS))
        if "G" in seen and "a" in seen[seen.index("G") :]:
            flips.append(seen[:40])
        while not pid_gone(pid):  # let it finish before the next round
            pass
    assert flips == []
