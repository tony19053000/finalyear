"""The subprocess runtime: the **unsandboxed** fallback (brief D51, ADR-0004).

.. warning::

   This runtime is **not a sandbox**. The workload runs as the agent's own user,
   can read and write everything that user can, sees every process, and uses the
   network. It exists for laptops without Docker, and the agent offers it only when
   the laptop's owner sets ``[agent.execution] allow_unsandboxed = true``
   (ADR-0014). ``docs/OPERATIONS.md`` says the same to operators.

What it does guarantee (same contract and the same test module as the Docker
runtime):

* the environment is exactly ``RunSpec.env`` (+ ``PYTHONPATH`` for the requirements,
  ``CUDA_VISIBLE_DEVICES`` / ``CUDA_DEVICE_ORDER`` for GPUs - an empty
  ``CUDA_VISIBLE_DEVICES`` when no GPU was asked for);
* **requirements (V0.5 D86):** with an environment builder, the job's requirements
  are installed **once per node** into a read-only ``--target`` dir cached by env
  hash (``envs/subprocess/<hash>``) and put on ``PYTHONPATH``; a second job with
  the same requirements installs nothing. Without a builder: the V0.4 per-job
  ``setup_argv`` install into ``<workdir>/.meshforge/deps``;
* ``cwd`` is the workdir; the child is the root of its own killable tree with
  parent-death cleanup and lowered priority (:mod:`meshforge.agent.process`); on
  Linux the group watchdog also kills the workload's **descendants** when the
  agent is SIGKILLed (V0.6 D59; a descendant that calls ``setsid`` escapes);
* the time limit (``deadline_exceeded`` within 2 s), cancellation (SIGTERM, SIGKILL
  after ``grace_s``, back within ``grace_s + 2 s``) and "no process survives" -
  leftover descendants in the group are killed after the workload exits;
* output bytes stream as they arrive, per stream in order, from one task.

Limits (V0.7 G5, D142 item 5; :class:`~.runtime.RunLimits` has the table):
on Linux ``limits.pids`` is ``RLIMIT_NPROC`` (per user: at most ``pids`` more
tasks than the agent's user had at exec) and, for an owner RAM cap
(``limits.enforce_memory``), ``limits.memory_bytes`` is ``RLIMIT_AS``
(address space - stricter than resident memory); on Windows the workload's job
object carries them (active processes, job memory, a hard CPU-rate cap for
``limits.cpus``). A required cap the job object cannot take refuses the run
(``config_error``, ``limits_not_applied``) rather than run unbounded.

On Linux ``limits.cpus`` is a real CPU share only when the agent runs in a
delegated cgroup v2 (:mod:`.cpu_cgroup`, V0.7 G6): each job then gets its own
cgroup whose ``cpu.max`` is its planned CPUs.

What it does **not** enforce: ``limits.cpus`` on Linux without such a cgroup
(the priority is lowered; advertised ``not_enforced``), the default memory
limit (no owner cap), network off, filesystem isolation. ``oom`` is
still **detected** (V0.6 D116, :mod:`.failure_evidence`): a workload that died of a
SIGKILL the agent did not send while the kernel's ``oom_kill`` counter moved, or
that exited with a Windows out-of-memory status, ends ``oom`` (``signal = 9`` is
kept); the executor adds the stderr-based ``MemoryError`` / CUDA rules.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from meshforge.agent.envs.errors import EnvError
from meshforge.agent.execution._pump import OutputPump
from meshforge.agent.execution.cpu_cgroup import CpuCgroups
from meshforge.agent.execution.failure_evidence import OomEvidence, windows_oom_exit
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
from meshforge.agent.process import ProcessHandle, ProcessLimits, SpawnError, spawn
from meshforge.agent.threads import uninterrupted
from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger

if TYPE_CHECKING:
    from meshforge.agent.envs.builder import BuiltEnv, EnvBuilder

__all__ = [
    "SUBPROCESS_RUNTIME_NAME",
    "EnvUse",
    "SubprocessRuntime",
    "pump_sink",
    "run_limits",
]

log = get_logger(__name__)

SUBPROCESS_RUNTIME_NAME: Final = "subprocess"
_READ_CHUNK: Final = 65536
_STOP_SLACK_S: Final = 2.0
_DRAIN_S: Final = 1.0
_PATH_SEP: Final = ";" if sys.platform == "win32" else ":"
_PYTHONS: Final = frozenset({"python", "python3"})
_SIGKILL: Final = int(getattr(signal, "SIGKILL", 9))


@dataclass(slots=True)
class _Exit:
    returncode: int | None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class EnvUse:
    """How a run uses a resolved cached environment."""

    built: BuiltEnv
    env: Mapping[str, str] = field(default_factory=dict)
    """Variables to *prepend* (``PATH``/``PYTHONPATH``, joined with the OS path
    separator) or set (anything else) in the workload's environment."""
    argv0: str | None = None
    """Replacement for a leading ``python``/``python3`` (the env's interpreter)."""


def _merge_env(env: dict[str, str], extra: Mapping[str, str]) -> None:
    for key, value in extra.items():
        if key in ("PATH", "PYTHONPATH") and env.get(key):
            env[key] = f"{value}{_PATH_SEP}{env[key]}"
        else:
            env[key] = value


class SubprocessRuntime:
    """Unsandboxed runtime on :mod:`meshforge.agent.process`. Runs may overlap."""

    name: ClassVar[str] = SUBPROCESS_RUNTIME_NAME

    def __init__(
        self,
        *,
        host_has_nvidia_gpu: bool = False,
        env_timeout_s: float = 1800.0,
        lower_priority: bool = True,
        clock: Callable[[], float] = time.monotonic,
        envs: EnvBuilder | None = None,
        cpu_cgroups: CpuCgroups | None = None,
    ) -> None:
        self._gpu = host_has_nvidia_gpu
        self._env_timeout_s = env_timeout_s
        self._lower_priority = lower_priority
        self._clock = clock
        self._envs = envs
        self._cgroups = cpu_cgroups

    # ------------------------------------------------------- cached envs
    def _uses_env_cache(self, spec: RunSpec) -> bool:
        """The subprocess runtime caches only when there is something to install."""
        return (
            self._envs is not None
            and spec.env_request is not None
            and spec.env_request.requirements is not None
        )

    async def _resolve_env(self, spec: RunSpec, pump: OutputPump) -> EnvUse:
        """The ``--target`` dir for the agent's interpreter (raises EnvError)."""
        envs, request = self._envs, spec.env_request
        if envs is None or request is None or request.requirements is None:
            raise RuntimeError("_resolve_env called without a builder or requirements")
        requirements = await asyncio.to_thread(
            envs.read_requirements, spec.workdir, request.requirements
        )
        built = await envs.ensure_target(
            requirements=requirements,
            deadline=envs.clock() + self._env_timeout_s,
            on_output=pump_sink(pump),
            log_fields={"attempt_id": spec.attempt_id, "runtime": self.name},
        )
        return EnvUse(built, env={"PYTHONPATH": str(built.path)})

    async def _env_phase(
        self, spec: RunSpec, pump: OutputPump, obs: RunObserver
    ) -> EnvUse | RunResult:
        """Resolve the cached env: an :class:`EnvUse`, or the failed result."""
        fields = {"attempt_id": spec.attempt_id, "runtime": self.name}
        try:
            try:
                use = await self._resolve_env(spec, pump)
            except OSError as exc:  # the envs dir itself (permissions, I/O)
                raise EnvError(
                    f"environment storage failed: {exc.strerror or type(exc).__name__}",
                    reason="env_io_failed",
                    failure_type=FailureType.INTERNAL_ERROR,
                ) from None
        except EnvError as exc:
            obs.finish()
            await pump.close(_DRAIN_S)
            log.warning(
                "agent.execution.run_failed",
                **fields,
                failure_type=exc.failure_type.value,
                reason=exc.reason,
            )
            return RunResult(
                exit_code=None,
                failure_type=exc.failure_type,
                reason=exc.reason,
                detail=exc.message,
                env_s=obs.env_s,
                env_hash=obs.env_hash or exc.env_hash,
            )
        obs.env_hash, obs.env_cache = use.built.env_hash, use.built.cache
        log.info(
            "agent.execution.env_ready",
            **fields,
            env_hash=use.built.env_hash,
            env_cache=use.built.cache,
            build_s=round(use.built.build_s, 3),
        )
        return use

    async def prepare_env(
        self, spec: RunSpec, on_output: OutputCallback
    ) -> tuple[str, str]:
        """Build (or find) the env of ``spec`` without running anything
        (``compute node prepare``, V0.5 G5): ``(env_hash, cache)``; raises
        :class:`EnvError` like a job's env phase would fail."""
        if not self._uses_env_cache(spec):
            raise EnvError(
                "this runtime has nothing to prepare without requirements",
                reason="requirements_missing",
            )
        pump = OutputPump(on_output, name=spec.attempt_id)
        pump.start()
        try:
            try:
                use = await self._resolve_env(spec, pump)
            except OSError as exc:
                raise EnvError(
                    f"environment storage failed: {exc.strerror or type(exc).__name__}",
                    reason="env_io_failed",
                    failure_type=FailureType.INTERNAL_ERROR,
                ) from None
        finally:
            await pump.close(_DRAIN_S)
        return use.built.env_hash, use.built.cache

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
        use: EnvUse | None = None
        try:
            if self._uses_env_cache(spec):
                obs.enter_env()
                resolved = await self._env_phase(spec, pump, obs)
                if isinstance(resolved, RunResult):
                    return resolved
                use = resolved
                if use.argv0 is not None and spec.argv[0] in _PYTHONS:
                    spec = dataclasses.replace(spec, argv=(use.argv0, *spec.argv[1:]))
            elif spec.setup_argv is not None:
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
            evidence = OomEvidence.snapshot()
            outcome = await self._step(spec, spec.argv, pump, obs, setup=False, use=use)
            obs.finish()
        except SpawnError as exc:
            in_setup = obs.phase is Phase.ENV
            obs.finish()
            await pump.close(_DRAIN_S)
            # the entrypoint is the user's; the setup command is ours (G4 builds it)
            spawn_failure = (
                FailureType.CONFIG_ERROR
                if exc.reason == "limits_not_applied"
                else FailureType.INTERNAL_ERROR
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
        # D116: a SIGKILL nobody here sent, while the kernel OOM-killed
        # something, is the OOM killer; so is a Windows out-of-memory status.
        oom_killed = windows_oom_exit(rc) or (
            sig == _SIGKILL
            and not outcome.timed_out
            and not consumer_failed
            and evidence.fired()
        )
        failure = classify(
            exit_code=exit_code,
            signal=sig,
            timed_out=outcome.timed_out,
            oom_killed=oom_killed,
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
            oom_killed=oom_killed,
            failure_type=failure,
            reason=reason or ("oom_killed" if oom_killed else None),
            detail=_describe(outcome, spec, sig, oom_killed=oom_killed),
            env_s=obs.env_s,
            run_s=obs.run_s,
            env_hash=obs.env_hash,
            env_cache=obs.env_cache,
        )
        log.info(
            "agent.execution.run_finished",
            **fields,
            exit_code=exit_code,
            signal=sig,
            failure_type=None if failure is None else failure.value,
            env_s=round(obs.env_s, 3),
            run_s=round(obs.run_s, 3),
            env_cache=obs.env_cache,
        )
        return result

    # ------------------------------------------------------------- internals
    def _env(
        self, spec: RunSpec, *, setup: bool, use: EnvUse | None = None
    ) -> dict[str, str]:
        env = dict(spec.env)
        if use is not None:
            _merge_env(env, use.env)
        elif spec.setup_argv is not None and not setup:
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
        use: EnvUse | None = None,
    ) -> _Exit:
        cgroup = None if setup else await self._job_cgroup(spec)
        try:
            handle = await spawn(
                argv,
                env=self._env(spec, setup=setup, use=use),
                cwd=spec.workdir,
                lower_priority=self._lower_priority,
                guard_group=True,  # D59: no grandchild outlives a killed agent
                limits=None if setup else run_limits(spec, cgroup=cgroup),
            )
        except BaseException:
            await self._drop_cgroup(cgroup)
            raise
        try:
            return await self._supervise(handle, spec, pump, obs, setup=setup)
        finally:
            await self._drop_cgroup(cgroup)

    async def _job_cgroup(self, spec: RunSpec) -> Path | None:
        """V0.7 G6: the job's CPU-share cgroup (Linux, delegated agent, a CPU
        cap planned). A cgroup the agent promised but cannot create refuses
        the run (``limits_not_applied``), never runs it uncapped."""
        if self._cgroups is None or spec.limits.cpus is None:
            return None
        try:
            # The thread always ends before we go on: a cancel during the
            # create must not lose the cgroup it makes (the T-14 class).
            return await uninterrupted(
                asyncio.to_thread(
                    self._cgroups.create, spec.attempt_id, spec.limits.cpus
                )
            )
        except asyncio.CancelledError:
            # By name: the create's result never reached us.
            await self._drop_cgroup(self._cgroups.path_for(spec.attempt_id))
            raise
        except (OSError, ValueError) as exc:
            raise SpawnError(
                f"cannot create the job's CPU cgroup: {type(exc).__name__}",
                reason="limits_not_applied",
            ) from exc

    async def _drop_cgroup(self, cgroup: Path | None) -> None:
        if cgroup is None or self._cgroups is None:
            return
        await asyncio.shield(asyncio.to_thread(self._cgroups.remove, cgroup))

    async def _supervise(
        self,
        handle: ProcessHandle,
        spec: RunSpec,
        pump: OutputPump,
        obs: RunObserver,
        *,
        setup: bool,
    ) -> _Exit:
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


def run_limits(spec: RunSpec, *, cgroup: Path | None = None) -> ProcessLimits:
    """The workload's process limits (the setup step gets none: it is ours).
    ``cgroup``: the job's CPU-share cgroup (V0.7 G6, Linux) the shim joins."""
    limits = spec.limits
    return ProcessLimits(
        memory_bytes=limits.memory_bytes if limits.enforce_memory else None,
        tasks=limits.pids,
        cpus=limits.cpus,
        required=limits.enforce_memory or cgroup is not None,
        cgroup=None if cgroup is None else str(cgroup),
    )


def pump_sink(pump: OutputPump) -> Callable[[str, bytes], Awaitable[None]]:
    """The builder's output goes to the job's log, like the V0.4 setup step's."""

    async def put(stream: str, data: bytes) -> None:
        await pump.put(Stream(stream), data)

    return put


async def _finish_readers(readers: list[asyncio.Task[None]]) -> None:
    try:
        async with asyncio.timeout(_DRAIN_S):
            await asyncio.gather(*readers, return_exceptions=True)
    except TimeoutError:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


def _describe(
    outcome: _Exit, spec: RunSpec, sig: int | None, *, oom_killed: bool = False
) -> str:
    if outcome.timed_out:
        return f"time limit of {spec.limits.timeout_s:g} s exceeded; stopped"
    if oom_killed:
        return "out of memory: killed by the operating system's OOM handling"
    if sig is not None:
        with contextlib.suppress(ValueError):
            return f"killed by signal {signal.Signals(sig).name}"
        return f"killed by signal {sig}"
    return f"exit code {outcome.returncode}"
