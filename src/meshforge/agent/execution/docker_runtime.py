"""The Docker runtime: the sandbox (brief D51, ADR-0004, roadmap 0.4.7 / 0.4.8).

Every workload container is created with (and each is covered by a sandbox test):

========================  =====================================================
``mounts``                the attempt workdir only, read-write at ``/work``
``user``                  numeric, never 0 (``[agent.execution] run_as``)
``cap_drop=ALL``          no capabilities (``CapEff = 0``)
``no-new-privileges``     set-uid binaries cannot regain any
``read_only``             root filesystem read-only; ``/tmp`` is a sized tmpfs
``pids_limit``            a fork bomb is contained
``mem_limit``             + ``memswap_limit`` equal to it (no swap): OOM -> ``oom``
``nano_cpus``             when the job has a CPU limit
``network_mode=none``     when ``RunSpec.network`` is false
``init``                  ``docker-init`` is PID 1: forwards SIGTERM, reaps zombies
``device_requests``       exactly the requested NVML indices; with no GPU asked
                          for, ``NVIDIA_VISIBLE_DEVICES=void`` too (an image that
                          sets ``=all`` under a default ``nvidia`` runtime gets none)
``log_config=none``       output is streamed over an attach made *before* start, so
                          nothing is kept in the daemon's log files (no disk fill)
``labels``                ``meshforge.*`` - orphans are swept at agent start
========================  =====================================================

Requirements (``RunSpec.setup_argv``) run first in a **setup container** with the
same hardening but the network on, writing into ``/work/.meshforge/deps``; the run
container gets that directory first on ``PYTHONPATH`` and keeps its own network
setting. The image pull and the setup container are the ``env`` phase, bounded by
``env_timeout_s`` (``timeout`` - retryable - when exceeded).

Environment added to ``RunSpec.env``: ``HOME=/tmp`` (unless given - the numeric
user has no home), ``PYTHONPATH`` (setup only), ``NVIDIA_VISIBLE_DEVICES=void``
(no GPU) or ``CUDA_DEVICE_ORDER=PCI_BUS_ID`` (GPU). A workload killed by signal N
shows exit code ``128 + N`` (``docker-init`` convention); ``RunResult.signal`` is
derived from that.

Blocking SDK calls run in threads. The output of a container is read by one daemon
thread per container (attach socket -> frames -> :class:`OutputPump`), which then
waits for the exit status; the event loop only awaits an event it sets.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from meshforge.agent.execution._pump import OutputPump
from meshforge.agent.execution.docker_support import (
    DOCKER_RUNTIME_NAME,
    LABEL_AGENT,
    LABEL_ATTEMPT,
    LABEL_MANAGED,
    LABEL_ROLE,
    docker_errors,
    docker_module,
    open_client,
    probe_docker,
    sweep_orphans,
)
from meshforge.agent.execution.runtime import (
    DEPS_SUBDIR,
    OutputCallback,
    Phase,
    RunObserver,
    RunResult,
    RunSpec,
    RuntimeAvailability,
    Stream,
    classify,
)
from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger

if TYPE_CHECKING:
    from docker import DockerClient
    from docker.models.containers import Container

    from meshforge.agent.settings import ExecutionSettings

__all__ = ["CONTAINER_WORKDIR", "DockerConfig", "DockerRuntime", "resolve_run_as"]

log = get_logger(__name__)

CONTAINER_WORKDIR: Final = "/work"
NOBODY: Final = "65534:65534"
_CONTAINER_TMP: Final = "/tmp"  # noqa: S108 - a private tmpfs inside the container
_READ_CHUNK: Final = 65536
_STOP_SLACK_S: Final = 2.0
"""After the SIGKILL, how long to wait for the exit before giving up (D51: the run
returns within ``grace_s + 2 s``)."""
_DRAIN_S: Final = 1.0
_WAIT_POLL_S: Final = 5
_STDOUT: Final = 1
_STDERR: Final = 2


def resolve_run_as(run_as: str) -> str:
    """``auto`` -> the agent's own ``uid:gid`` on POSIX when it is not root (so the
    workload can write its workdir and the agent can read the outputs), else
    ``65534:65534`` (nobody). An explicit ``uid:gid`` is used as given."""
    if run_as != "auto":
        return run_as
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return NOBODY
    uid, gid = os.geteuid(), os.getegid()
    if uid == 0 or gid == 0:
        return NOBODY
    return f"{uid}:{gid}"


@dataclass(frozen=True, slots=True)
class DockerConfig:
    """What the runtime needs from ``[agent.execution]`` and from discovery."""

    agent_label: str
    """Label value marking this agent's containers (the node id)."""
    run_as: str = "auto"
    tmp_size_mb: int = 512
    shm_size_mb: int = 512
    env_timeout_s: float = 1800.0
    api_timeout_s: float = 30.0
    host_has_nvidia_gpu: bool = False
    gpu_probe_image: str | None = None

    @classmethod
    def from_settings(
        cls, settings: ExecutionSettings, *, agent_label: str, host_has_nvidia_gpu: bool
    ) -> DockerConfig:
        return cls(
            agent_label=agent_label,
            run_as=settings.run_as,
            tmp_size_mb=settings.tmp_size_mb,
            shm_size_mb=settings.shm_size_mb,
            env_timeout_s=settings.env_timeout_s,
            api_timeout_s=settings.docker_timeout_s,
            host_has_nvidia_gpu=host_has_nvidia_gpu,
            gpu_probe_image=settings.images.python_cpu,
        )


class _FaultError(Exception):
    """An infrastructure failure that ends the run with a typed result."""

    def __init__(self, failure_type: FailureType, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.failure_type = failure_type
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class _Exit:
    exit_code: int | None
    oom_killed: bool
    timed_out: bool = False
    stopped: bool = False
    """We stopped it (timeout, output consumer failure) - not its own exit."""


class _Watcher:
    """One daemon thread: container output -> pump, then the exit status."""

    def __init__(
        self,
        client: DockerClient,
        container_id: str,
        sock: Any,
        pump: OutputPump,
        api_timeout_s: float,
    ) -> None:
        self._client = client
        self._id = container_id
        self._sock = sock
        self._pump = pump
        self._api_timeout = api_timeout_s
        self.stop = threading.Event()
        self.exit_code: int | None = None
        self.stream_error: str | None = None
        self._loop = asyncio.get_running_loop()
        self.done = asyncio.Event()
        self.first_output_at: float | None = None
        self._thread = threading.Thread(
            target=self._main, name=f"docker-watch:{container_id[:12]}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _main(self) -> None:
        try:
            self._read_output()
            self._wait_exit()
        finally:
            with contextlib.suppress(RuntimeError):  # loop already closed
                self._loop.call_soon_threadsafe(self.done.set)

    def _read_output(self) -> None:
        streams = {_STDOUT: Stream.STDOUT, _STDERR: Stream.STDERR}
        try:
            for stream_id, chunk in _frames(self._sock):
                stream = streams.get(stream_id)
                if stream is None:
                    continue
                if self.first_output_at is None:
                    self.first_output_at = time.monotonic()
                if not self._pump.put_from_thread(stream, chunk, self.stop):
                    return
        except Exception as exc:  # noqa: BLE001 - output lost, the exit still counts
            if not self.stop.is_set():
                self.stream_error = type(exc).__name__
        finally:
            _close_socket(self._sock)

    def _wait_exit(self) -> None:
        errors = docker_errors()
        while not self.stop.is_set():
            try:
                status = self._client.api.wait(self._id, timeout=_WAIT_POLL_S)
            except errors as exc:
                if _is_not_found(exc):
                    return
                if _is_timeout(exc):
                    continue
                self.stream_error = self.stream_error or type(exc).__name__
                return
            code = status.get("StatusCode") if isinstance(status, Mapping) else None
            self.exit_code = int(code) if isinstance(code, int) else None
            return

    def close(self) -> None:
        self.stop.set()
        _close_socket(self._sock)


def _is_not_found(exc: BaseException) -> bool:
    docker = docker_module()
    return docker is not None and isinstance(exc, docker.errors.NotFound)


def _is_timeout(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "timeout" in text or "timed out" in text


def _frames(sock: Any) -> Iterator[tuple[int, bytes]]:
    """The attach stream's multiplexed frames, read in chunks of at most 64 KiB (a
    frame header announcing gigabytes never makes us allocate them at once)."""
    from docker.utils.socket import SocketError, read, read_exactly

    while True:
        try:
            header = read_exactly(sock, 8)
        except SocketError:
            return
        stream_id, size = struct.unpack(">BxxxL", header)
        while size > 0:
            chunk = read(sock, min(size, _READ_CHUNK))
            if not chunk:
                return
            size -= len(chunk)
            yield stream_id, bytes(chunk)


def _close_socket(sock: Any) -> None:
    """Close the attach stream *and* the socket under it (``SocketIO.close`` alone
    leaves the unix socket open until garbage collection)."""
    raw = getattr(sock, "_sock", sock)
    with contextlib.suppress(Exception):
        raw.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        sock.close()
    with contextlib.suppress(Exception):
        raw.close()
    response = getattr(sock, "_response", None) or getattr(raw, "_response", None)
    if response is not None:
        with contextlib.suppress(Exception):
            response.close()


def _pull_ref(image: str) -> tuple[str, str]:
    """``name[:tag]@sha256:d`` -> ``(name, "sha256:d")``; ``name:tag`` -> ``(name, tag)``."""
    name, at, digest = image.partition("@")
    if at:
        repo = name
        slash = repo.rfind("/")
        colon = repo.rfind(":")
        if colon > slash:
            repo = repo[:colon]
        return repo, digest
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1 :]
    return name, "latest"


class DockerRuntime:
    """The sandboxed runtime. One instance per agent; runs may overlap."""

    name: Final = DOCKER_RUNTIME_NAME

    def __init__(
        self,
        config: DockerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock
        self._background: set[asyncio.Task[None]] = set()
        self._user = resolve_run_as(config.run_as)

    # ------------------------------------------------------------ probe/sweep
    async def probe(self) -> RuntimeAvailability:
        return await asyncio.to_thread(
            probe_docker,
            timeout_s=self.config.api_timeout_s,
            host_has_nvidia_gpu=self.config.host_has_nvidia_gpu,
            gpu_probe_image=self.config.gpu_probe_image,
            agent_label=self.config.agent_label,
        )

    async def sweep_orphans(self) -> int:
        """Remove containers a previous (killed) agent with this label left behind."""

        def sweep() -> int:
            client = open_client(self.config.api_timeout_s)
            try:
                return sweep_orphans(client, self.config.agent_label)
            finally:
                client.close()

        removed = await asyncio.to_thread(sweep)
        if removed:
            log.warning("agent.execution.orphans_swept", removed=removed)
        return removed

    # ------------------------------------------------------------------- run
    async def run(
        self,
        spec: RunSpec,
        on_output: OutputCallback,
        *,
        observer: RunObserver | None = None,
    ) -> RunResult:
        obs = observer if observer is not None else RunObserver(clock=self._clock)
        if spec.image is None:
            return _fault_result(
                obs,
                FailureType.INTERNAL_ERROR,
                "no_image",
                "the Docker runtime needs an image",
            )
        if not spec.workdir.is_dir():
            return _fault_result(
                obs,
                FailureType.INTERNAL_ERROR,
                "workdir_missing",
                "the workdir does not exist",
            )
        fields = {"attempt_id": spec.attempt_id, "runtime": self.name}
        pump = OutputPump(on_output, name=spec.attempt_id)
        pump.start()
        client: DockerClient | None = None
        obs.enter_env()
        try:
            try:
                client = await asyncio.to_thread(open_client, self.config.api_timeout_s)
            except Exception as exc:
                raise _FaultError(
                    FailureType.ENV_ERROR,
                    "docker_unavailable",
                    f"cannot reach the Docker daemon: {type(exc).__name__}",
                ) from exc
            outcome = await self._steps(client, spec, spec.image, pump, obs, fields)
        except _FaultError as fault:
            obs.finish()
            await pump.close(_DRAIN_S)
            log.warning(
                "agent.execution.run_failed",
                **fields,
                failure_type=fault.failure_type.value,
                reason=fault.reason,
            )
            return RunResult(
                exit_code=obs.exit_code,
                failure_type=fault.failure_type,
                reason=fault.reason,
                detail=fault.detail,
                env_s=obs.env_s,
                run_s=obs.run_s,
            )
        except asyncio.CancelledError:
            obs.finish()
            await pump.abort()
            log.info("agent.execution.run_cancelled", **fields, phase=obs.phase.value)
            raise
        finally:
            if client is not None:
                await self._close_client(client)
        await pump.close(_DRAIN_S)
        result = _result(outcome, spec, obs)
        log.info(
            "agent.execution.run_finished",
            **fields,
            exit_code=result.exit_code,
            failure_type=None
            if result.failure_type is None
            else result.failure_type.value,
            env_s=round(obs.env_s, 3),
            run_s=round(obs.run_s, 3),
        )
        return result

    async def _steps(
        self,
        client: DockerClient,
        spec: RunSpec,
        image: str,
        pump: OutputPump,
        obs: RunObserver,
        fields: Mapping[str, Any],
    ) -> _Exit:
        """Image, setup container, run container. Raises :class:`_FaultError`."""
        self._prepare_workdir(spec)
        env_deadline = self._clock() + self.config.env_timeout_s
        await self._ensure_image(client, image, env_deadline, fields)
        if spec.setup_argv is not None:
            setup = await self._container(
                client, spec, image, pump, obs, role="setup", deadline=env_deadline
            )
            if setup.timed_out:
                raise _FaultError(
                    FailureType.TIMEOUT,
                    "env_timeout",
                    f"requirements install exceeded {self.config.env_timeout_s:g} s",
                )
            if setup.oom_killed or setup.exit_code != 0:
                raise _FaultError(
                    FailureType.ENV_ERROR,
                    "setup_failed",
                    f"requirements install failed (exit {setup.exit_code}"
                    f"{', out of memory' if setup.oom_killed else ''})",
                )
        obs.enter_run()
        # deadline None: the time limit starts when the run container has started
        outcome = await self._container(
            client, spec, image, pump, obs, role="run", deadline=None
        )
        obs.finish()
        if pump.failed is not None:
            raise _FaultError(
                FailureType.INTERNAL_ERROR,
                "output_consumer_failed",
                f"on_output raised {type(pump.failed).__name__}",
            )
        return outcome

    # ------------------------------------------------------------- internals
    def _prepare_workdir(self, spec: RunSpec) -> None:
        if self._user == NOBODY and sys.platform != "win32":
            # The workload is not the workdir's owner: let it write there.
            with contextlib.suppress(OSError):
                spec.workdir.chmod(0o777)

    async def _close_client(self, client: DockerClient) -> None:
        task = asyncio.ensure_future(asyncio.to_thread(client.close))
        with contextlib.suppress(Exception):
            await asyncio.shield(task)

    async def _ensure_image(
        self,
        client: DockerClient,
        image: str,
        deadline: float,
        fields: Mapping[str, Any],
    ) -> None:
        errors = docker_errors()
        docker = docker_module()
        try:
            await asyncio.to_thread(client.images.get, image)
        except errors as exc:
            if docker is None or not isinstance(exc, docker.errors.ImageNotFound):
                raise _FaultError(
                    FailureType.ENV_ERROR, "docker_unavailable", type(exc).__name__
                ) from exc
        else:
            return
        log.info("agent.execution.image_pull", **fields, image=image)
        stop = threading.Event()
        repo, tag = _pull_ref(image)

        def pull() -> str | None:
            for message in client.api.pull(repo, tag=tag, stream=True, decode=True):
                if stop.is_set():
                    return "cancelled"
                if isinstance(message, Mapping) and message.get("error"):
                    return str(message.get("error"))[:200]
            return None

        task = asyncio.ensure_future(asyncio.to_thread(pull))
        try:
            async with asyncio.timeout(max(0.0, deadline - self._clock())):
                error = await asyncio.shield(task)
        except TimeoutError:
            stop.set()
            raise _FaultError(
                FailureType.TIMEOUT,
                "env_timeout",
                f"image pull exceeded {self.config.env_timeout_s:g} s",
            ) from None
        except asyncio.CancelledError:
            stop.set()
            raise
        except errors as exc:
            raise _FaultError(
                FailureType.ENV_ERROR, "image_pull_failed", type(exc).__name__
            ) from exc
        if error is not None:
            raise _FaultError(FailureType.ENV_ERROR, "image_pull_failed", error)

    def _create_kwargs(self, docker: Any, spec: RunSpec, role: str) -> dict[str, Any]:
        setup = role == "setup"
        env = dict(spec.env)
        env.setdefault("HOME", _CONTAINER_TMP)
        if spec.setup_argv is not None and not setup:
            deps = f"{CONTAINER_WORKDIR}/{DEPS_SUBDIR}"
            env["PYTHONPATH"] = (
                f"{deps}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else deps
            )
        gpus = spec.gpu_indices if not setup else ()
        device_requests = []
        if gpus:
            env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            device_requests = [
                docker.types.DeviceRequest(
                    driver="nvidia",
                    device_ids=[str(i) for i in gpus],
                    capabilities=[["gpu"]],
                )
            ]
        else:
            env["NVIDIA_VISIBLE_DEVICES"] = "void"
        limits = spec.limits
        kwargs: dict[str, Any] = {
            "command": list(
                spec.setup_argv if setup and spec.setup_argv else spec.argv
            ),
            "name": f"mf-{spec.attempt_id.lower()}-{role}",
            "hostname": "meshforge-job",
            "user": self._user,
            "working_dir": CONTAINER_WORKDIR,
            "environment": env,
            "labels": {
                LABEL_MANAGED: "1",
                LABEL_AGENT: self.config.agent_label,
                LABEL_ATTEMPT: spec.attempt_id,
                LABEL_ROLE: role,
            },
            "mounts": [
                docker.types.Mount(
                    target=CONTAINER_WORKDIR,
                    source=str(spec.workdir),
                    type="bind",
                    read_only=False,
                )
            ],
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "read_only": True,
            "tmpfs": {
                _CONTAINER_TMP: f"rw,nosuid,nodev,size={self.config.tmp_size_mb}m"
            },
            "shm_size": f"{self.config.shm_size_mb}m",
            "pids_limit": limits.pids,
            "mem_limit": limits.memory_bytes,
            "memswap_limit": limits.memory_bytes,
            "network_mode": "bridge" if (setup or spec.network) else "none",
            "init": True,
            "log_config": docker.types.LogConfig(type="none"),
            "stdin_open": False,
            "tty": False,
        }
        if limits.cpus is not None:
            kwargs["nano_cpus"] = int(limits.cpus * 1e9)
        if device_requests:
            kwargs["device_requests"] = device_requests
        return kwargs

    async def _container(
        self,
        client: DockerClient,
        spec: RunSpec,
        image: str,
        pump: OutputPump,
        obs: RunObserver,
        *,
        role: str,
        deadline: float | None,
    ) -> _Exit:
        """Create, attach, start and wait for one container; always remove it."""
        errors = docker_errors()
        docker = docker_module()
        kwargs = self._create_kwargs(docker, spec, role)
        container: Container | None = None
        watcher: _Watcher | None = None
        try:
            try:
                container = await asyncio.to_thread(
                    client.containers.create, image, **kwargs
                )
            except errors as exc:
                raise _create_fault(exc, docker, role) from exc
            container_id = str(container.id)
            obs.ident = container_id[:12]
            sock = await asyncio.to_thread(
                client.api.attach_socket,
                container_id,
                params={"stdout": 1, "stderr": 1, "stream": 1, "logs": 0},
            )
            watcher = _Watcher(
                client, container_id, sock, pump, self.config.api_timeout_s
            )
            watcher.start()
            try:
                await asyncio.to_thread(container.start)
            except errors as exc:
                raise _FaultError(
                    FailureType.ENV_ERROR
                    if role == "setup"
                    else FailureType.INTERNAL_ERROR,
                    "container_start_failed",
                    _short(exc),
                ) from exc
            if role == "run":
                obs.run_started = self._clock()  # the limit counts from the real start
                deadline = self._clock() + spec.limits.timeout_s
            outcome = await self._await_exit(
                container, watcher, pump, obs, spec, deadline
            )
        except asyncio.CancelledError:
            obs.phase = Phase.STOPPING
            if container is not None:
                await self._shielded(self._stop(container, watcher, spec.grace_s))
            raise
        finally:
            if watcher is not None:
                watcher.close()
            if container is not None:
                await self._shielded(self._remove(container))
        obs.exit_code = outcome.exit_code
        obs.signal = _signal_of(outcome.exit_code)
        return outcome

    async def _await_exit(
        self,
        container: Container,
        watcher: _Watcher,
        pump: OutputPump,
        obs: RunObserver,
        spec: RunSpec,
        deadline: float | None,
    ) -> _Exit:
        exited = asyncio.ensure_future(watcher.done.wait())
        failed = asyncio.ensure_future(pump.failed_event.wait())
        try:
            timeout = None if deadline is None else max(0.0, deadline - self._clock())
            done, _ = await asyncio.wait(
                {exited, failed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for fut in (exited, failed):
                fut.cancel()
            await asyncio.gather(exited, failed, return_exceptions=True)
        timed_out = not done
        stopped = timed_out or (failed in done and exited not in done)
        if stopped:
            obs.phase = Phase.STOPPING
            await self._stop(container, watcher, spec.grace_s)
        state = await self._state(container)
        exit_code = watcher.exit_code
        if exit_code is None and isinstance(state.get("ExitCode"), int):
            exit_code = int(state["ExitCode"])
        if watcher.stream_error:
            log.warning(
                "agent.execution.output_stream_error",
                attempt_id=spec.attempt_id,
                cause=watcher.stream_error,
            )
        if obs.phase in (Phase.RUN, Phase.STOPPING):
            obs.leave_run()
        return _Exit(
            exit_code=exit_code,
            oom_killed=bool(state.get("OOMKilled")),
            timed_out=timed_out,
            stopped=stopped,
        )

    async def _stop(
        self, container: Container, watcher: _Watcher | None, grace_s: float
    ) -> None:
        """SIGTERM, then SIGKILL after ``grace_s``; returns within grace + 2 s."""
        with contextlib.suppress(*docker_errors()):
            await asyncio.to_thread(container.kill, signal="SIGTERM")
        if await self._exited_within(container, watcher, grace_s):
            return
        with contextlib.suppress(*docker_errors()):
            await asyncio.to_thread(container.kill, signal="SIGKILL")
        await self._exited_within(container, watcher, _STOP_SLACK_S)

    async def _exited_within(
        self, container: Container, watcher: _Watcher | None, seconds: float
    ) -> bool:
        if watcher is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(watcher.done.wait(), seconds)
                return True
            return False
        state = await self._state(container)
        return not state.get("Running", False)

    async def _state(self, container: Container) -> dict[str, Any]:
        try:
            await asyncio.to_thread(container.reload)
        except docker_errors():
            return {}
        state = container.attrs.get("State")
        return state if isinstance(state, dict) else {}

    async def _remove(self, container: Container) -> None:
        try:
            await asyncio.to_thread(container.remove, force=True)
        except docker_errors() as exc:
            if not _is_not_found(exc):
                log.warning(
                    "agent.execution.container_remove_failed",
                    container=container.short_id,
                    cause=type(exc).__name__,
                )

    async def _shielded(self, coro: Any) -> None:
        """Run a cleanup step to completion even if we are cancelled again; the task
        is kept (never fire-and-forget) and its failure is logged."""
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._cleanup_done)
        await asyncio.shield(task)

    def _cleanup_done(self, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "agent.execution.cleanup_failed",
                cause=type(task.exception()).__name__,
            )


def _create_fault(exc: BaseException, docker: Any, role: str) -> _FaultError:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 409:
        return _FaultError(
            FailureType.CONFLICT,
            "duplicate_run",
            f"a {role} container for this attempt already exists",
        )
    text = str(exc).lower()
    if docker is not None and isinstance(exc, docker.errors.ImageNotFound):
        return _FaultError(FailureType.ENV_ERROR, "image_missing", _short(exc))
    if "could not select device driver" in text or "nvidia" in text:
        return _FaultError(FailureType.ENV_ERROR, "gpu_unavailable", _short(exc))
    return _FaultError(
        FailureType.INTERNAL_ERROR, "container_create_failed", _short(exc)
    )


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _result(outcome: _Exit, spec: RunSpec, obs: RunObserver) -> RunResult:
    signal_no = _signal_of(outcome.exit_code)
    failure = classify(
        exit_code=outcome.exit_code,
        signal=signal_no,
        timed_out=outcome.timed_out,
        oom_killed=outcome.oom_killed,
    )
    unknown = outcome.exit_code is None and failure is None
    return RunResult(
        exit_code=outcome.exit_code,
        signal=signal_no,
        timed_out=outcome.timed_out,
        oom_killed=outcome.oom_killed,
        failure_type=FailureType.INTERNAL_ERROR if unknown else failure,
        reason="exit_status_unknown" if unknown else None,
        detail=_describe(outcome, spec),
        env_s=obs.env_s,
        run_s=obs.run_s,
    )


def _signal_of(exit_code: int | None) -> int | None:
    if exit_code is not None and 128 < exit_code <= 128 + 64:
        return exit_code - 128
    return None


def _describe(outcome: _Exit, spec: RunSpec) -> str:
    if outcome.timed_out:
        return f"time limit of {spec.limits.timeout_s:g} s exceeded; stopped"
    if outcome.oom_killed:
        return f"out of memory (limit {spec.limits.memory_bytes} bytes)"
    if outcome.exit_code is None:
        return "the container's exit status could not be read"
    return f"exit code {outcome.exit_code}"


def _fault_result(
    obs: RunObserver, failure_type: FailureType, reason: str, detail: str
) -> RunResult:
    obs.finish()
    return RunResult(
        exit_code=None, failure_type=failure_type, reason=reason, detail=detail
    )


def workdir_for(root: Path, attempt_id: str) -> Path:
    """``<workdir_root>/<attempt_id>`` - the convention G4's executor uses."""
    return root / attempt_id
