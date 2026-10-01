"""Run the environment tools (``uv``, an interpreter) as killable child processes.

Every child is the root of its own process tree (:mod:`meshforge.agent.process`):
a deadline or a cancellation ends the whole tree (SIGTERM, then SIGKILL after a
grace period), so a stuck ``pip``/``uv`` or a hostile ``setup.py`` cannot outlive
the build. The child's environment is an explicit allowlist (:func:`tool_env`):
build scripts of the user's packages never see the agent's own secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from meshforge.agent.envs.errors import EnvError
from meshforge.agent.process import ProcessHandle, SpawnError, spawn
from meshforge.common.errors import FailureType

__all__ = [
    "OutputSink",
    "capture_tool",
    "run_tool",
    "tool_env",
    "venv_bin",
    "venv_python",
]

OutputSink = Callable[[str, bytes], Awaitable[None]]
"""``(stream, data)`` with stream ``"stdout"`` or ``"stderr"`` - the build's output
goes to the job that triggered it (its log), like the V0.4 setup step's did."""

_READ_CHUNK: Final = 65536
_GRACE_S: Final = 3.0
_DRAIN_S: Final = 1.0
_CAPTURE_MAX: Final = 65536
_PASS_THROUGH: Final = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LOCALAPPDATA",
    "APPDATA",
    "USERPROFILE",
    "PROGRAMDATA",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def tool_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted environment for a tool child (+ ``extra``).

    Nothing else from the agent's environment passes - in particular no
    ``MESHFORGE_*`` (tokens), ``UV_*``/``PIP_*`` (an operator's personal index or
    config must not change what an env hash builds) or ``PYTHON*``/``VIRTUAL_ENV``.
    """
    env = {name: os.environ[name] for name in _PASS_THROUGH if os.environ.get(name)}
    env.setdefault("PATH", os.defpath)
    if extra:
        env.update(extra)
    return env


def _timeout(what: str) -> EnvError:
    return EnvError(
        f"{what} exceeded the environment budget ([agent.execution] env_timeout_s)",
        reason="env_timeout",
        failure_type=FailureType.TIMEOUT,
    )


async def _spawn(
    argv: Sequence[str], env: Mapping[str, str], cwd: Path | None
) -> ProcessHandle:
    try:
        return await spawn(argv, env=env, cwd=cwd, lower_priority=True)
    except SpawnError as exc:
        raise EnvError(
            f"cannot start {Path(argv[0]).name!r}: {exc.reason}",
            reason="uv_missing",
        ) from None


async def _pipe(
    reader: asyncio.StreamReader | None, stream: str, sink: OutputSink | None
) -> None:
    if reader is None:
        return
    broken = False
    with contextlib.suppress(OSError, ValueError):
        while chunk := await reader.read(_READ_CHUNK):
            if sink is None or broken:
                continue
            try:
                await sink(stream, chunk)
            except Exception:  # noqa: BLE001 - a dead consumer must not stall the child
                broken = True


async def _finish(handle: ProcessHandle, readers: list[asyncio.Task[None]]) -> None:
    handle.kill_now()  # "no process survives": descendants too
    try:
        async with asyncio.timeout(_DRAIN_S):
            await asyncio.gather(*readers, return_exceptions=True)
    except TimeoutError:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
    await handle.reap()


async def _stop(handle: ProcessHandle) -> None:
    handle.terminate_group()
    if await handle.wait(_GRACE_S) is None:
        handle.kill_now()
        await handle.wait(_GRACE_S)


async def run_tool(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    deadline: float,
    on_output: OutputSink | None = None,
    cwd: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    what: str = "the environment build",
) -> int:
    """Run ``argv`` to completion; its exit code. Output streams to ``on_output``.

    Raises :class:`EnvError`: ``uv_missing`` (cannot start), ``env_timeout``
    (``deadline`` - monotonic - passed; the tree is stopped). Cancellation stops
    the tree before ``CancelledError`` propagates.
    """
    remaining = deadline - clock()
    if remaining <= 0:
        raise _timeout(what)
    handle = await _spawn(argv, env, cwd)
    readers = [
        asyncio.create_task(_pipe(handle.proc.stdout, "stdout", on_output)),
        asyncio.create_task(_pipe(handle.proc.stderr, "stderr", on_output)),
    ]
    try:
        code = await handle.wait(remaining)
        if code is None:
            await _stop(handle)
            raise _timeout(what)
    except asyncio.CancelledError:
        await asyncio.shield(_stop(handle))
        raise
    finally:
        await asyncio.shield(_finish(handle, readers))
    return code


async def capture_tool(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    cwd: Path | None = None,
    what: str = "a tool",
) -> tuple[int, bytes]:
    """Run a short query (``uv python find``, an interpreter probe): exit code and
    the first 64 KiB of stdout. Same tree handling as :func:`run_tool`."""
    collected = bytearray()

    async def keep(stream: str, data: bytes) -> None:
        if stream == "stdout" and len(collected) < _CAPTURE_MAX:
            collected.extend(data[: _CAPTURE_MAX - len(collected)])

    clock = time.monotonic
    code = await run_tool(
        argv,
        env=env,
        deadline=clock() + timeout_s,
        on_output=keep,
        cwd=cwd,
        clock=clock,
        what=what,
    )
    return code, bytes(collected)


def venv_python(env_dir: Path) -> Path:
    """The interpreter inside a venv directory (``Scripts\\python.exe`` on Windows)."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def venv_bin(env_dir: Path) -> Path:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return env_dir / "Scripts"
    return env_dir / "bin"
