r"""The venv runtime: an **unsandboxed** process in a cached uv venv (ADR-0004, V0.5 D86).

Same process handling, limits and contract as
:class:`~meshforge.agent.execution.subprocess_runtime.SubprocessRuntime` (it *is*
one, with a different environment), so the same warning applies: **not a
sandbox** - offered only with ``[agent.execution] allow_unsandboxed = true``.

What it adds over the plain subprocess runtime:

* the workload runs in a **uv venv built once per env hash** (``envs/venv/<hash>``,
  read-only), always - with or without requirements - so it gets the manifest's
  ``env.python`` version and *only* the job's packages, never the agent's own
  site-packages (the subprocess runtime runs on the agent's interpreter);
* a leading ``python``/``python3`` in the entrypoint is the venv's interpreter,
  and the venv's ``bin`` (``Scripts`` on Windows) is first on ``PATH``
  (``VIRTUAL_ENV`` set), so console scripts of installed packages resolve too.

Selection order (:func:`~meshforge.agent.execution.selection.choose_runtime`):
docker > venv > subprocess - and only for the ``python-cpu`` base: a fresh venv has
no PyTorch, so ``cuda-pytorch`` jobs go to Docker or the subprocess runtime (the
latter only with torch in the agent's Python, D432). A GPU job on a laptop without
Docker uses this runtime with ``env.base: python-cpu``, ``resources.gpu:
required`` and CUDA torch in its requirements (V1.0 G9-B). The
probe says ``available`` only when the pinned ``uv`` binary (the ``uv`` wheel, agent
extra) runs.

**One worker process on Windows (V1.0 G10 BUG-4, D451).** There a venv's
``Scripts\python.exe`` is not the interpreter but a launcher that starts the
base interpreter (``pyvenv.cfg`` ``home``) as a second process with
``__PYVENV_LAUNCHER__`` set, and waits for it (lab L4: the worker appeared 17 s
after the launcher, then hung). The runtime does the launcher's job itself: it
starts the base interpreter directly with that variable (CPython ``getpath``:
``sys.executable`` becomes the venv's python and the venv is found next to it -
same ``sys.prefix``, same site-packages, :func:`direct_python`). One process -
the one the job object, the CPU sampler and the stall watch see. Anything
unusual (no ``pyvenv.cfg``, no ``home``, a missing interpreter) keeps the
launcher. The ``home`` read here is the venv's own base interpreter: since
V1.0 G11 (D458) the env hash names that interpreter and a venv whose ``home`` is
gone or moved is rebuilt before it gets here, so an upgraded agent never starts
the previous version's ``python.exe``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Final

from meshforge.agent.envs._tool import capture_tool, tool_env, venv_bin
from meshforge.agent.envs.base_python import pyvenv_home
from meshforge.agent.envs.builder import EnvBuilder
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.execution._pump import OutputPump
from meshforge.agent.execution.cpu_cgroup import CpuCgroups
from meshforge.agent.execution.runtime import RunSpec, RuntimeAvailability
from meshforge.agent.execution.subprocess_runtime import (
    EnvUse,
    SubprocessRuntime,
    pump_sink,
)

__all__ = ["PYVENV_LAUNCHER_VAR", "VENV_RUNTIME_NAME", "VenvRuntime", "direct_python"]

VENV_RUNTIME_NAME: Final = "venv"
PYVENV_LAUNCHER_VAR: Final = "__PYVENV_LAUNCHER__"
"""What CPython's venv launcher sets for the base interpreter it starts (D451)."""
_PROBE_TIMEOUT_S: Final = 15.0


def direct_python(
    venv: Path, *, platform: str = sys.platform
) -> tuple[Path, dict[str, str]] | None:
    """Windows (D451): ``(base interpreter, env)`` that runs as ``venv`` without
    the launcher process, or None (not Windows, or anything unusual: keep the
    launcher)."""
    if platform != "win32":
        return None
    launcher = venv / "Scripts" / "python.exe"
    home = pyvenv_home(venv)
    if home is None or not launcher.is_file():
        return None
    base = home / "python.exe"
    if not base.is_file():
        return None
    return base, {PYVENV_LAUNCHER_VAR: str(launcher)}


class VenvRuntime(SubprocessRuntime):
    """See module docstring. Runs may overlap."""

    name: ClassVar[str] = VENV_RUNTIME_NAME

    def __init__(
        self,
        *,
        envs: EnvBuilder,
        host_has_nvidia_gpu: bool = False,
        env_timeout_s: float = 1800.0,
        lower_priority: bool = True,
        clock: Callable[[], float] = time.monotonic,
        cpu_cgroups: CpuCgroups | None = None,
    ) -> None:
        super().__init__(
            host_has_nvidia_gpu=host_has_nvidia_gpu,
            env_timeout_s=env_timeout_s,
            lower_priority=lower_priority,
            clock=clock,
            envs=envs,
            cpu_cgroups=cpu_cgroups,
        )
        self._builder = envs

    async def probe(self) -> RuntimeAvailability:
        uv = self._builder.uv_argv
        if not uv:
            return RuntimeAvailability(self.name, False, reason="uv_missing")
        try:
            code, out = await capture_tool(
                [*uv, "--version"], env=tool_env(), timeout_s=_PROBE_TIMEOUT_S
            )
        except (EnvError, OSError):
            return RuntimeAvailability(self.name, False, reason="uv_missing")
        if code != 0:
            return RuntimeAvailability(self.name, False, reason="uv_failed")
        return RuntimeAvailability(
            self.name,
            True,
            gpu_capable=self._gpu,
            gpu_reason="host NVIDIA GPU" if self._gpu else "no_nvidia_gpu",
            version=out.decode("utf-8", "replace").strip()[:64],
            sandboxed=False,
        )

    def _uses_env_cache(self, spec: RunSpec) -> bool:
        return True  # always a venv, even without requirements

    async def _resolve_env(self, spec: RunSpec, pump: OutputPump) -> EnvUse:
        request = spec.env_request
        requirements = b""
        if request is not None and request.requirements is not None:
            requirements = await asyncio.to_thread(
                self._builder.read_requirements, spec.workdir, request.requirements
            )
        built = await self._builder.ensure_venv(
            python=None if request is None else request.python,
            requirements=requirements,
            deadline=self._builder.clock() + self._env_timeout_s,
            on_output=pump_sink(pump),
            log_fields={"attempt_id": spec.attempt_id, "runtime": self.name},
        )
        env = {"PATH": str(venv_bin(built.path)), "VIRTUAL_ENV": str(built.path)}
        argv0 = str(built.python)
        direct = await asyncio.to_thread(direct_python, built.path)
        if direct is not None:  # D451: one process on Windows, no launcher hop
            argv0 = str(direct[0])
            env.update(direct[1])
        return EnvUse(built, env=env, argv0=argv0)
