"""The cluster harness never leaks a process (roadmap 0.3.14, containment).

A leaked ``meshforge-agent`` keeps dialling a proxy port that a later test may
reuse, and a leaked controller holds a database connection that blocks the
per-test ``DROP DATABASE``; either would make the tier flaky in ways that look
like product bugs. These tests kill things for real and check with psutil.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

import mf_cluster
import psutil  # type: ignore[import-untyped]  # no stubs; agent extra
from mf_cluster import FAST, Fabric, ManagedProcess

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

SUPPORT_DIR = Path(__file__).resolve().parents[1] / "_support"
GONE_WITHIN_S = 10.0


def _wait_gone(pids: list[int], within: float = GONE_WITHIN_S) -> list[int]:
    """Pids still running (zombies count as gone) after ``within`` seconds."""
    deadline = time.monotonic() + within
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [p for p in alive if _running(p)]
        time.sleep(0.05)
    return alive


def _running(pid: int) -> bool:
    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


class _BoomError(RuntimeError):
    pass


@pytest.mark.docker
def test_fabric_leaks_nothing_when_the_test_body_raises(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    pids: list[int] = []
    ports: list[int] = []

    def body() -> None:
        with Fabric(pg_settings, tmp_path, agents=2, timing=FAST) as f:
            pids.extend(f.all_pids())
            ports.extend(n.link.address[1] for n in f.nodes)
            assert len(pids) >= 3  # controller + 2 agents (+ any descendants)
            raise _BoomError

    with pytest.raises(_BoomError):
        body()
    assert _wait_gone(pids) == []
    for port in ports:  # the proxy's listeners are closed too
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=2)


def test_teardown_kills_descendants_even_in_another_session(tmp_path: Path) -> None:
    """A grandchild that left the process group is still found and killed."""
    script = textwrap.dedent(
        """
        import os, subprocess, sys, time
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
        )
        print(child.pid, flush=True)
        time.sleep(120)
        """
    )
    proc = ManagedProcess("parent", ["-c", script], dict(os.environ), tmp_path)
    proc.start()
    try:
        deadline = time.monotonic() + 10
        while not (lines := proc.log.tail()) or not lines[0].strip().isdigit():
            assert time.monotonic() < deadline, lines
            time.sleep(0.05)
        grandchild = int(lines[0])
        assert _running(grandchild)
        assert proc.terminate_tree() == []
        assert _wait_gone([proc.pid, grandchild]) == []
    finally:
        proc.terminate_tree()


def test_atexit_backstop_kills_registered_processes(tmp_path: Path) -> None:
    proc = ManagedProcess(
        "sleeper", ["-c", "import time; time.sleep(120)"], dict(os.environ), tmp_path
    )
    proc.start()
    assert proc in mf_cluster._REGISTRY
    mf_cluster._kill_registered()  # what the atexit hook runs
    assert _wait_gone([proc.pid]) == []
    assert proc not in mf_cluster._REGISTRY


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only"
)
def test_children_die_with_a_sigkilled_test_process(tmp_path: Path) -> None:
    """The test process itself is SIGKILLed (no finalizer, no atexit): the kernel
    still kills the harness's children via the PDEATHSIG launcher."""
    driver = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(SUPPORT_DIR)!r})
        from pathlib import Path
        from mf_cluster import ManagedProcess
        proc = ManagedProcess(
            "orphan", ["-c", "import time; time.sleep(120)"],
            dict(os.environ), Path({str(tmp_path)!r}),
        )
        proc.start()
        print(proc.pid, flush=True)
        time.sleep(120)
        """
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", driver], stdout=subprocess.PIPE, text=True
    )
    try:
        assert parent.stdout is not None
        child = int(parent.stdout.readline())
        # The launcher execs the real child in place: same pid, now sleeping.
        deadline = time.monotonic() + 10
        while "sleep(120)" not in " ".join(psutil.Process(child).cmdline()):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(10)
        assert _wait_gone([child]) == [], "child survived its SIGKILLed parent"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(10)
        if parent.stdout is not None:
            parent.stdout.close()
