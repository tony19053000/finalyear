"""V0.6 G1 (D59): no grandchild of a subprocess-runtime workload outlives a
SIGKILLed agent on Linux.

``PR_SET_PDEATHSIG`` covers the workload process only; before V0.6 its own
children (a sleeper it forked) were orphaned when the agent died. The exec
shim's group watchdog now kills the whole process group when the workload
process exits. Every check is by pid through psutil, against real processes:
an "agent" process runs the real :class:`SubprocessRuntime`, the test SIGKILLs
it (no cleanup code can run) and asserts that nothing it started survives.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

psutil = pytest.importorskip("psutil", reason="needs the agent extra (pid checks)")

from meshforge.agent.execution.runtime import RunLimits, RunSpec  # noqa: E402
from meshforge.agent.execution.subprocess_runtime import (  # noqa: E402
    SubprocessRuntime,
)
from meshforge.common.ids import new_attempt_id  # noqa: E402

LINUX = sys.platform.startswith("linux")
pytestmark = [
    pytest.mark.timeout(120),
    pytest.mark.skipif(
        not LINUX,
        reason="the group watchdog is Linux; Windows uses a kill-on-close job "
        "object instead (not verifiable here)",
    ),
]

PY = sys.executable

#: The workload: forks a sleeper (same process group), records both pids,
#: then sleeps. ``os.wait()`` first proves the watchdog is not its child.
WORKLOAD = textwrap.dedent(
    """
    import os, subprocess, sys, time
    try:
        os.wait()
        raise SystemExit("unexpected child before the workload started any")
    except ChildProcessError:
        pass
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(sys.argv[1] + ".tmp", "w") as fh:
        fh.write(f"{os.getpid()} {g.pid}")
    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(600)
    """
)

#: The "agent": runs the workload through the real SubprocessRuntime.
AGENT = textwrap.dedent(
    """
    import asyncio, os, sys
    from pathlib import Path
    from meshforge.agent.execution.runtime import RunLimits, RunSpec
    from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
    from meshforge.common.ids import new_attempt_id

    async def main():
        workdir = Path(sys.argv[1])
        spec = RunSpec(
            attempt_id=new_attempt_id(),
            argv=(sys.executable, "-c", sys.argv[2], sys.argv[3]),
            workdir=workdir,
            env={"PATH": os.environ.get("PATH", "")},
            limits=RunLimits(memory_bytes=256 << 20, pids=64, timeout_s=600),
        )
        async def sink(stream, data):
            return None
        print("running", flush=True)
        await SubprocessRuntime().run(spec, sink)

    asyncio.run(main())
    """
)


def _alive(pid: int) -> bool:
    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def _gone(pids: list[int], timeout_s: float) -> list[int]:
    deadline = time.monotonic() + timeout_s
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    return [p for p in pids if _alive(p)]


def _wait_file(path: Path, timeout_s: float = 20.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists() and (text := path.read_text().strip()):
            return text
        time.sleep(0.02)
    raise AssertionError(f"{path.name} was never written")


def _group_members(pgid: int) -> list[int]:
    members = []
    for proc in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(proc.info["pid"]) == pgid and _alive(proc.info["pid"]):
                members.append(proc.info["pid"])
        except (ProcessLookupError, PermissionError):
            continue
    return members


def test_sigkill_of_the_agent_kills_the_workloads_grandchildren(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "pids.txt"
    workdir = tmp_path / "work"
    workdir.mkdir()
    with subprocess.Popen(
        [PY, "-c", AGENT, str(workdir), WORKLOAD, str(pidfile)],
        stdout=subprocess.PIPE,
        text=True,
    ) as agent:
        survivors: list[int] = []
        try:
            workload, grandchild = map(int, _wait_file(pidfile).split())
            assert _alive(workload) and _alive(grandchild)
            group = os.getpgid(workload)
            assert os.getpgid(grandchild) == group
            # Exactly one more member: the watchdog (not the workload's child).
            extra = set(_group_members(group)) - {workload, grandchild}
            assert len(extra) == 1, extra
            watchdog = extra.pop()
            assert psutil.Process(watchdog).ppid() != workload
            agent.send_signal(signal.SIGKILL)
            agent.wait(10)
            survivors = _gone([workload, grandchild, watchdog], timeout_s=5.0)
            assert survivors == [], "a process outlived the SIGKILLed agent"
        finally:
            for pid in survivors:  # never leak past a failing run
                with contextlib.suppress(psutil.Error):
                    psutil.Process(pid).kill()
            agent.kill()
            agent.wait(10)


def test_a_normal_run_is_unchanged_and_leaves_nothing(tmp_path: Path) -> None:
    """While the agent lives the watchdog changes nothing: exit code, output and
    'no process survives' are as before, and the run is not slowed by it."""
    pidfile = tmp_path / "sleeper.pid"
    code = (
        "import subprocess, sys; "
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
        f"open({str(pidfile)!r}, 'w').write(str(g.pid)); "
        "print('hello'); sys.exit(3)"
    )
    out: list[bytes] = []

    async def sink(_stream: object, data: bytes) -> None:
        out.append(data)

    async def scenario() -> tuple[int | None, float]:
        spec = RunSpec(
            attempt_id=new_attempt_id(),
            argv=(PY, "-c", code),
            workdir=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            limits=RunLimits(memory_bytes=256 << 20, pids=64, timeout_s=60),
        )
        started = time.monotonic()
        result = await SubprocessRuntime().run(spec, sink)
        return result.exit_code, time.monotonic() - started

    exit_code, took = asyncio.run(scenario())
    assert exit_code == 3
    assert b"".join(out).strip() == b"hello"
    assert took < 10
    assert _gone([int(_wait_file(pidfile))], timeout_s=3.0) == []
