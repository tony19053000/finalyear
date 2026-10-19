"""V0.9 G5 lane B, tester round 1: the CPU rule's exclusions with REAL processes.

Brief: non-fabric CPU > N % counts as owner activity, "excluding the agent's own
process tree, burners and job processes". A busy child of this process (what a
job / burner is to the agent) must not count; a busy process that is NOT in the
tree (re-parented away, like the owner's own program) must.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time

import pytest

pytest.importorskip("psutil", reason="the agent extra")

import psutil

from meshforge.agent.owner_activity import CpuSampler

pytestmark = [
    pytest.mark.component,
    pytest.mark.timeout(120),
    pytest.mark.skipif(sys.platform == "win32", reason="re-parenting trick is POSIX"),
]

BUSY = "import time\nt=time.time()+25\nwhile time.time()<t: pass\n"


def _share(percent: float | None) -> float:
    """Percent of ONE core, as a share of the whole machine."""
    assert percent is not None
    return percent


def test_a_busy_child_is_excluded_and_a_busy_stranger_is_counted() -> None:
    cpus = psutil.cpu_count() or 1
    one_core = 100.0 / cpus
    sampler = CpuSampler()
    child = subprocess.Popen([sys.executable, "-c", BUSY])
    stranger_pid: int | None = None
    try:
        sampler.sample()  # prime
        time.sleep(2.0)
        mine = _share(sampler.sample())
        assert mine < 0.7 * one_core, f"a child of the agent counted: {mine:.1f} %"

        # a process outside the tree: double fork so init/subreaper adopts it
        launcher = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys,subprocess\n"
                "if os.fork()==0:\n"
                "    os.setsid()\n"
                "    D=subprocess.DEVNULL\n"
                "    p=subprocess.Popen([sys.executable,'-c',sys.argv[1]],"
                "stdin=D,stdout=D,stderr=D)\n"
                "    print(p.pid, flush=True)\n"
                "    os._exit(0)\n"
                "os.wait()\n",
                BUSY,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        stranger_pid = int(launcher.stdout.split()[0])
        assert psutil.Process(stranger_pid).ppid() != child.pid
        sampler.sample()
        time.sleep(2.0)
        theirs = _share(sampler.sample())
        assert theirs > mine + 0.5 * one_core, (
            f"a busy process outside the tree was not counted: {theirs:.1f} % "
            f"(one core = {one_core:.1f} %)"
        )
    finally:
        child.kill()
        child.wait()
        if stranger_pid is not None:
            with contextlib.suppress(psutil.Error):
                psutil.Process(stranger_pid).kill()
