"""V1.0 G7 tester: the supervisor's grandchild sweep, hammered.

The flaky original asserted once after a wait loop. Here the same scenario runs many
times (and with a great-grandchild), and every pid is asserted gone with the shared
``pid_gone`` rule, then re-asserted after a settle so a gone -> alive flip would show.
"""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from meshforge.agent.winservice import StopKind
from meshforge.agent.winservice_supervisor import Supervisor

pytest.importorskip("psutil", reason="needs the agent extra")
from mf_procs import pid_gone

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups"),
]

ROUNDS = 12


@pytest.mark.parametrize("depth", [1, 2])
def test_swept_repeatedly_and_never_flips_back_to_alive(
    tmp_path: Path, depth: int
) -> None:
    for n in range(ROUNDS):
        child = tmp_path / f"c{n}.py"
        if depth == 1:
            body = """
            import subprocess, sys
            k = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            print("pids", k.pid, flush=True)
            sys.exit(1)
            """
        else:
            body = """
            import subprocess, sys
            inner = ("import subprocess, sys, time; "
                     "g = subprocess.Popen([sys.executable, '-c', "
                     "'import time; time.sleep(600)']); "
                     "print(g.pid, flush=True); time.sleep(600)")
            k = subprocess.Popen([sys.executable, "-c", inner], stdout=subprocess.PIPE)
            g = k.stdout.readline().decode().strip()
            print("pids", k.pid, g, flush=True)
            sys.exit(1)
            """
        child.write_text(textwrap.dedent(body), encoding="utf-8")
        log = tmp_path / f"a{n}.log"
        decision = Supervisor(
            [sys.executable, str(child)], log_path=log, grace_s=5
        ).run()
        assert decision.kind is StopKind.RESTART
        pids = [
            int(p) for p in log.read_text(encoding="utf-8").split("pids ", 1)[1].split()
        ]
        assert len(pids) == depth
        end = time.monotonic() + 10
        while not all(pid_gone(p) for p in pids) and time.monotonic() < end:
            time.sleep(0.02)
        assert all(pid_gone(p) for p in pids), (n, pids)
        time.sleep(0.05)
        assert all(pid_gone(p) for p in pids), ("flipped back", n, pids)
