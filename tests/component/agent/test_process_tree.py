"""V0.4 G3 (roadmap 0.4.17 items 1, 3, 6): ``meshforge.agent.process`` against real
processes - tree kill, parent-death cleanup, the spawn cancel window, the exact
environment. Every check is by pid through psutil; nothing is mocked.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

psutil = pytest.importorskip("psutil", reason="needs the agent extra (pid checks)")

from meshforge.agent.process import (  # noqa: E402 - after the importorskip guard
    DEATH_SIGNAL_SUPPORTED,
    NICE,
    SpawnError,
    resolve_executable,
    spawn,
)

pytestmark = pytest.mark.timeout(120)

LINUX = sys.platform.startswith("linux")
POSIX = sys.platform != "win32"
PY = sys.executable
DEVNULL = asyncio.subprocess.DEVNULL

#: A child that starts a grandchild (both sleep), writing the grandchild's pid.
TREE = textwrap.dedent(
    """
    import subprocess, sys, time
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    open(sys.argv[1], "w").write(str(g.pid))
    time.sleep(600)
    """
)


def _env(**extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    env.update(extra)
    return {k: v for k, v in env.items() if v}


def _alive(pid: int) -> bool:
    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def _gone(pids: list[int], timeout_s: float = 3.0) -> list[int]:
    deadline = time.monotonic() + timeout_s
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    return [p for p in pids if _alive(p)]


def _wait_file(path: Path, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists() and (text := path.read_text().strip()):
            return text
        time.sleep(0.02)
    raise AssertionError(f"{path.name} was never written")


# ------------------------------------------------------------------ basics
def test_the_child_gets_exactly_the_given_environment() -> None:
    """Nothing of the agent's environment leaks; on POSIX ``env`` (not a Python,
    which would add PEP 538's LC_CTYPE itself) proves the set is exact."""
    os.environ["MF_AGENT_SECRET_SENTINEL"] = "leak"
    argv = (
        ["env"]
        if POSIX
        else [
            PY,
            "-I",
            "-c",
            "import os; [print(f'{k}={v}') for k, v in os.environ.items()]",
        ]
    )
    try:

        async def scenario() -> bytes:
            handle = await spawn(argv, env=_env(ONLY_THIS="1"))
            assert handle.proc.stdout is not None
            out = await handle.proc.stdout.read()
            await handle.reap()
            return out

        lines = asyncio.run(scenario()).decode().splitlines()
    finally:
        del os.environ["MF_AGENT_SECRET_SENTINEL"]
    seen = {line.split("=", 1)[0].upper() for line in lines if "=" in line}
    assert "MF_AGENT_SECRET_SENTINEL" not in seen
    assert "ONLY_THIS" in seen
    if POSIX:
        assert seen == set(_env(ONLY_THIS="1"))


def test_a_missing_executable_is_a_typed_error_not_exit_127() -> None:
    with pytest.raises(SpawnError) as err:
        asyncio.run(spawn(["no-such-program-mf"], env=_env()))
    assert err.value.reason == "executable_not_found"
    with pytest.raises(SpawnError):
        resolve_executable("python", {"PATH": ""})


def test_the_pid_is_the_programs_pid_and_argv_is_untouched(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, bytes]:
        pids: list[int] = []
        handle = await spawn(
            [
                PY,
                "-c",
                "import os, sys; print(os.getpid(), sys.argv[1:])",
                "a b",
                "--x",
            ],
            env=_env(),
            on_spawn=pids.append,
        )
        assert handle.proc.stdout is not None
        out = await handle.proc.stdout.read()
        await handle.reap()
        assert pids == [handle.pid]
        return handle.pid, out

    pid, out = asyncio.run(scenario())
    assert out.decode().split(" ", 1)[0] == str(pid)
    assert "['a b', '--x']" in out.decode()


@pytest.mark.skipif(not LINUX, reason="niceness via the Linux exec shim")
def test_lower_priority_is_applied_on_linux() -> None:
    async def scenario() -> bytes:
        handle = await spawn(
            [PY, "-c", "import os; print(os.nice(0))"], env=_env(), lower_priority=True
        )
        assert handle.proc.stdout is not None
        out = await handle.proc.stdout.read()
        await handle.reap()
        return out

    assert int(asyncio.run(scenario())) >= NICE


# ------------------------------------------------------------- tree kill
def test_kill_now_ends_the_whole_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "g.pid"

    async def scenario() -> list[int]:
        handle = await spawn(
            [PY, "-c", TREE, str(pidfile)], env=_env(), stdout=DEVNULL, stderr=DEVNULL
        )
        grandchild = int(await asyncio.to_thread(_wait_file, pidfile))
        handle.kill_now()
        await handle.reap()
        assert handle.killed
        return [handle.pid, grandchild]

    pids = asyncio.run(scenario())
    assert _gone(pids) == []


@pytest.mark.skipif(not POSIX, reason="SIGTERM to the process group is POSIX")
def test_terminate_group_reaches_the_grandchild(tmp_path: Path) -> None:
    pidfile = tmp_path / "g.pid"

    async def scenario() -> list[int]:
        handle = await spawn(
            [PY, "-c", TREE, str(pidfile)], env=_env(), stdout=DEVNULL, stderr=DEVNULL
        )
        grandchild = int(await asyncio.to_thread(_wait_file, pidfile))
        handle.terminate_group()
        code = await handle.wait(5.0)
        assert code == -signal.SIGTERM
        await handle.reap()
        return [handle.pid, grandchild]

    assert _gone(asyncio.run(scenario())) == []


# ------------------------------------------------------------ parent death
_PARENT = textwrap.dedent(
    """
    import asyncio, os, sys, time
    from meshforge.agent.process import spawn

    async def main():
        env = {"PATH": os.environ.get("PATH", "")}
        h = await spawn([sys.executable, "-c", "import time; time.sleep(600)"],
                        env=env, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL)
        print(h.pid, flush=True)
        await asyncio.sleep(600)

    asyncio.run(main())
    """
)


@pytest.mark.skipif(
    not DEATH_SIGNAL_SUPPORTED or not POSIX, reason="pdeathsig is Linux"
)
def test_sigkill_of_the_parent_kills_the_child() -> None:
    """Item 6: the agent is SIGKILLed (no cleanup code runs) -> its child dies too."""
    with subprocess.Popen(
        [PY, "-c", _PARENT], stdout=subprocess.PIPE, text=True
    ) as parent:
        try:
            assert parent.stdout is not None
            child = int(parent.stdout.readline())
            assert _alive(child)
            parent.send_signal(signal.SIGKILL)
            parent.wait(10)
            survivors = _gone([child], timeout_s=3.0)
            for pid in survivors:  # never leak it past a failing run
                psutil.Process(pid).kill()
            assert survivors == [], "the child outlived its parent"
        finally:
            parent.kill()
            parent.wait(10)


# ----------------------------------------------------------- cancel window
@pytest.mark.parametrize("yields", [0, 1, 2, 3, 5, 8, 13])
def test_cancelling_during_spawn_leaves_no_child(tmp_path: Path, yields: int) -> None:
    """A cancel landing anywhere inside spawn() kills what it had started."""
    pidfile = tmp_path / "c.pid"
    code = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(600)"

    async def scenario() -> None:
        task = asyncio.create_task(
            spawn([PY, "-c", code], env=_env(), stdout=DEVNULL, stderr=DEVNULL)
        )
        for _ in range(yields):
            await asyncio.sleep(0)
        if task.done():
            handle = task.result()  # spawn won the race: this is the normal path
            handle.kill_now()
            await handle.reap()
            return
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    time.sleep(0.5)  # a leaked child would have written its pid by now
    if pidfile.exists() and pidfile.read_text():
        survivors = _gone([int(pidfile.read_text())], timeout_s=1.0)
        for pid in survivors:  # never leak it past a failing run
            psutil.Process(pid).kill()
        assert survivors == [], "a child started by a cancelled spawn survived"


# ---------------------------------------------------------------- Windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows job object")
def test_windows_job_object_kills_the_tree_and_closes_on_release(
    tmp_path: Path,
) -> None:  # pragma: no cover - Windows only
    pidfile = tmp_path / "g.pid"

    async def scenario() -> list[int]:
        handle = await spawn(
            [PY, "-c", TREE, str(pidfile)], env=_env(), stdout=DEVNULL, stderr=DEVNULL
        )
        assert handle.uses_job_object
        grandchild = int(await asyncio.to_thread(_wait_file, pidfile))
        handle.release()  # closing the kill-on-close job ends the whole tree
        await handle.wait(5.0)
        return [handle.pid, grandchild]

    assert _gone(asyncio.run(scenario())) == []
