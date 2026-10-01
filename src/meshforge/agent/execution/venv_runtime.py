"""The venv runtime: an **unsandboxed** process in a cached uv venv (ADR-0004, V0.5 D86).

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
docker > venv > subprocess. The probe says ``available`` only when the pinned
``uv`` binary (the ``uv`` wheel, agent extra) runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import ClassVar, Final

from meshforge.agent.envs._tool import capture_tool, tool_env, venv_bin
from meshforge.agent.envs.builder import EnvBuilder
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.execution._pump import OutputPump
from meshforge.agent.execution.runtime import RunSpec, RuntimeAvailability
from meshforge.agent.execution.subprocess_runtime import (
    EnvUse,
    SubprocessRuntime,
    pump_sink,
)

__all__ = ["VENV_RUNTIME_NAME", "VenvRuntime"]

VENV_RUNTIME_NAME: Final = "venv"
_PROBE_TIMEOUT_S: Final = 15.0


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
    ) -> None:
        super().__init__(
            host_has_nvidia_gpu=host_has_nvidia_gpu,
            env_timeout_s=env_timeout_s,
            lower_priority=lower_priority,
            clock=clock,
            envs=envs,
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
        return EnvUse(
            built,
            env={"PATH": str(venv_bin(built.path)), "VIRTUAL_ENV": str(built.path)},
            argv0=str(built.python),
        )
