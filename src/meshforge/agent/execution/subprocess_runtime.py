"""The subprocess runtime: the **unsandboxed** fallback (brief D51, ADR-0004).

.. warning::

   This runtime is **not a sandbox**. The workload runs as the agent's own user,
   can read and write everything that user can, sees every process, and uses the
   network. It exists for laptops without Docker, and the agent offers it only when
   the laptop's owner sets ``[agent.execution] allow_unsandboxed = true``
   (ADR-0014). ``docs/OPERATIONS.md`` says the same to operators.

What it does guarantee (same contract and the same test module as the Docker
runtime):

* the environment is exactly ``RunSpec.env`` (+ ``PYTHONPATH`` for the deps dir when
  there is a setup step, ``CUDA_VISIBLE_DEVICES`` / ``CUDA_DEVICE_ORDER`` for GPUs -
  an empty ``CUDA_VISIBLE_DEVICES`` when no GPU was asked for);
* ``cwd`` is the workdir; the child is the root of its own killable tree with
  parent-death cleanup and lowered priority (:mod:`meshforge.agent.process`);
* the time limit (``deadline_exceeded`` within 2 s), cancellation (SIGTERM, SIGKILL
  after ``grace_s``, back within ``grace_s + 2 s``) and "no process survives" -
  leftover descendants in the group are killed after the workload exits;
* output bytes stream as they arrive, per stream in order, from one task.

What it does **not** enforce: ``limits.memory_bytes``, ``limits.pids`` and
``limits.cpus`` (no cgroups here), network off, filesystem isolation. ``oom`` can
therefore not be detected - a workload the kernel OOM-kills ends ``user_error``
with ``signal = 9``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from meshforge.agent.execution._pump import OutputPump
from meshforge.agent.execution.runtime import (
    OutputCallback,
    Phase,
    RunObserver,
    RunResult,
    RunSpec,
    RuntimeAvailability,
    Stream,
    classify,
)
from meshforge.agent.process import ProcessHandle, SpawnError, spawn
from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger

__all__ = ["SUBPROCESS_RUNTIME_NAME", "SubprocessRuntime"]

log = get_logger(__name__)

SUBPROCESS_RUNTIME_NAME: Final = "subprocess"
_READ_CHUNK: Final = 65536
_STOP_SLACK_S: Final = 2.0
_DRAIN_S: Final = 1.0
_PATH_SEP: Final = ";" if sys.platform == "win32" else ":"


@dataclass(slots=True)
class _Exit:
    returncode: int | None
    timed_out: bool = False


class SubprocessRuntime:
    """Unsandboxed runtime on :mod:`meshforge.agent.process`. Runs may overlap."""

    name: Final = SUBPROCESS_RUNTIME_NAME

    def __init__(
        self,
        *,
        host_has_nvidia_gpu: bool = False,
        env_timeout_s: float = 1800.0,
        lower_priority: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gpu = host_has_nvidia_gpu
        self._env_timeout_s = env_timeout_s
        self._lower_priority = lower_priority
        self._clock = clock

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability(
            self.name,
            True,
            gpu_capable=self._gpu,
            gpu_reason="host NVIDIA GPU" if self._gpu else "no_nvidia_gpu",
            version=f"python{sys.version_info[0]}.{sys.version_info[1]}",
            sandboxed=False,
        )

    async def run(
        self,
        spec: RunSpec,
        on_output: OutputCallback,
        *,
        observer: RunObserver | None = None,
    ) -> RunResult:
        obs = observer if observer is not None else RunObserver(clock=self._clock)
        fields = {"attempt_id": spec.attempt_id, "runtime": self.name}
        if not spec.workdir.is_dir():
            obs.finish()
            return RunResult(
                exit_code=None,
                failure_type=FailureType.INTERNAL_ERROR,
                reason="workdir_missing",
                detail="the workdir does not exist",
            )
        pump = OutputPump(on_output, name=spec.attempt_id)
        pump.start()
        try:
            if spec.setup_argv is not None:
                obs.enter_env()
                setup = await self._step(spec, spec.setup_argv, pump, obs, setup=True)
                if setup.timed_out or setup.returncode != 0:
                    obs.finish()
                    await pump.close(_DRAIN_S)
                    timed_out = setup.timed_out
                    return RunResult(
                        exit_code=setup.returncode,
                        failure_type=FailureType.TIMEOUT
                        if timed_out
                        else FailureType.ENV_ERROR,
                        reason="env_timeout" if timed_out else "setup_failed",
                        detail=f"requirements install "
                        f"{'timed out' if timed_out else f'failed (exit {setup.returncode})'}",
                        env_s=obs.env_s,
                    )
            obs.enter_run()
            outcome = await self._step(spec, spec.argv, pump, obs, setup=False)
            obs.finish()
        except SpawnError as exc:
            in_setup = obs.phase is Phase.ENV
            obs.finish()
            await pump.close(_DRAIN_S)
            # the entrypoint is the user's; the setup command is ours (G4 builds it)
            spawn_failure = (
                FailureType.INTERNAL_ERROR
                if exc.reason != "executable_not_found"
                else FailureType.ENV_ERROR
                if in_setup
                else FailureType.USER_ERROR
            )
            log.warning(
                "agent.execution.run_failed",
                **fields,
                failure_type=spawn_failure.value,
                reason=exc.reason,
            )
            return RunResult(
                exit_code=None,
                failure_type=spawn_failure,
                reason=exc.reason,
                detail=exc.message,
                env_s=obs.env_s,
            )
        except asyncio.CancelledError:
            obs.finish()
            await pump.abort()
            log.info("agent.execution.run_cancelled", **fields, phase=obs.phase.value)
            raise
        consumer_failed = pump.failed is not None
        await pump.close(_DRAIN_S)
        rc = outcome.returncode
        sig = -rc if rc is not None and rc < 0 else None
        exit_code = rc if rc is not None and rc >= 0 else None
        failure = classify(
            exit_code=exit_code,
            signal=sig,
            timed_out=outcome.timed_out,
            oom_killed=False,
        )
        reason = None
        if consumer_failed:
            failure, reason = FailureType.INTERNAL_ERROR, "output_consumer_failed"
        elif rc is None:
            failure, reason = FailureType.INTERNAL_ERROR, "exit_status_unknown"
        result = RunResult(
            exit_code=exit_code,
            signal=sig,
            timed_out=outcome.timed_out,
            failure_type=failure,
            reason=reason,
            detail=_describe(outcome, spec, sig),
            env_s=obs.env_s,
            run_s=obs.run_s,
        )
        log.info(
            "agent.execution.run_finished",
            **fields,
            exit_code=exit_code,
            signal=sig,
            failure_type=None if failure is None else failure.value,
            env_s=round(obs.env_s, 3),
            run_s=round(obs.run_s, 3),
        )
        return result

    # ------------------------------------------------------------- internals
    def _env(self, spec: RunSpec, *, setup: bool) -> dict[str, str]:
        env = dict(spec.env)
        if spec.setup_argv is not None and not setup:
            deps = str(spec.deps_dir)
            old = env.get("PYTHONPATH")
            env["PYTHONPATH"] = f"{deps}{_PATH_SEP}{old}" if old else deps
        if spec.gpu_indices and not setup:
            env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in spec.gpu_indices)
        else:
            env["CUDA_VISIBLE_DEVICES"] = ""
        return env

    async def _step(
        self,
        spec: RunSpec,
        argv: tuple[str, ...],
        pump: OutputPump,
        obs: RunObserver,
        *,
        setup: bool,
    ) -> _Exit:
        handle = await spawn(
            argv,
            env=self._env(spec, setup=setup),
            cwd=spec.workdir,
            lower_priority=self._lower_priority,
        )
        obs.ident = str(handle.pid)
        if not setup:
            obs.run_started = self._clock()
        readers = [
            asyncio.create_task(self._read(handle, Stream.STDOUT, pump)),
            asyncio.create_task(self._read(handle, Stream.STDERR, pump)),
        ]
        limit = self._env_timeout_s if setup else spec.limits.timeout_s
        deadline = self._clock() + limit
        try:
            timed_out = await self._wait(handle, pump, deadline)
            if timed_out or pump.failed is not None:
                obs.phase = Phase.STOPPING
                await self._stop(handle, spec.grace_s)
        except asyncio.CancelledError:
            obs.phase = Phase.STOPPING
            await asyncio.shield(self._stop(handle, spec.grace_s))
            raise
        finally:
            # "No process survives": descendants still in the group die now, and
            # that also closes the pipes they held, so the readers reach EOF.
            handle.kill_now()
            await _finish_readers(readers)
            await handle.reap()
        if not setup:
            obs.leave_run()
        else:
            obs.leave_env()
        obs.exit_code = handle.returncode
        return _Exit(handle.returncode, timed_out)

    async def _read(
        self, handle: ProcessHandle, stream: Stream, pump: OutputPump
    ) -> None:
        pipe = handle.proc.stdout if stream is Stream.STDOUT else handle.proc.stderr
        if pipe is None:
            return
        with contextlib.suppress(OSError, ValueError):
            while chunk := await pipe.read(_READ_CHUNK):
                await pump.put(stream, chunk)

    async def _wait(
        self, handle: ProcessHandle, pump: OutputPump, deadline: float
    ) -> bool:
        """Wait for the exit; True if the deadline passed first. A failed output
        consumer also ends the wait (the caller stops the workload)."""
        exited = asyncio.ensure_future(handle.proc.wait())
        failed = asyncio.ensure_future(pump.failed_event.wait())
        try:
            done, _ = await asyncio.wait(
                {exited, failed},
                timeout=max(0.0, deadline - self._clock()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for fut in (exited, failed):
                fut.cancel()
            await asyncio.gather(exited, failed, return_exceptions=True)
        return not done

    async def _stop(self, handle: ProcessHandle, grace_s: float) -> None:
        """SIGTERM the group, SIGKILL it after ``grace_s``."""
        handle.terminate_group()
        if await handle.wait(grace_s) is not None:
            return
        handle.kill_now()
        await handle.wait(_STOP_SLACK_S)


async def _finish_readers(readers: list[asyncio.Task[None]]) -> None:
    try:
        async with asyncio.timeout(_DRAIN_S):
            await asyncio.gather(*readers, return_exceptions=True)
    except TimeoutError:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


def _describe(outcome: _Exit, spec: RunSpec, sig: int | None) -> str:
    if outcome.timed_out:
        return f"time limit of {spec.limits.timeout_s:g} s exceeded; stopped"
    if sig is not None:
        with contextlib.suppress(ValueError):
            return f"killed by signal {signal.Signals(sig).name}"
        return f"killed by signal {sig}"
    return f"exit code {outcome.returncode}"
