"""Controller-driven preparation of a node (V0.5 G5, roadmap 0.5.8 controller part).

``compute node prepare`` sends ``PrepareEnv{request_id, base_images, envs}``;
this service pulls the pinned base images (the same code as the local
``meshforge-agent prepare``) and builds each environment through the runtime a
job would use (docker > venv > subprocess, or the one asked for) - the exact
G2 build path, so a prepared env is the env the job then finds (``hit``).

Progress and the result go back as ``PrepareEnvStatus`` reports (buffered,
replayed after a reconnect, acked): one when the request is accepted (every
item ``pending``), one per finished item, and a last one with ``done=True``.
Every item ends ``hit`` (already there), ``pulled`` / ``built`` (done now) or
``failed`` with a typed ``failure_type`` + ``reason`` (``docker_unavailable``,
``runtime_unavailable``, ``requirements_rejected``, a pull ``timeout``, ...).

Idempotent: the same ``request_id`` again (a replayed command) does not run a
second time - the last status is re-sent; a new request finds what an earlier
one built and reports ``hit``. The command is network input: at most
:data:`MAX_IMAGES` bases (known names only), :data:`MAX_ENVS` envs, each
requirements file within ``[agent.envs] max_requirements_bytes`` and normalised
(local paths refused) before anything runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import normalize_requirements
from meshforge.agent.envs.prepare import BASES, PrepareReport, prepare
from meshforge.agent.execution.runtime import (
    EnvRequest,
    RunLimits,
    RunSpec,
    Stream,
)
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

if TYPE_CHECKING:
    from meshforge.agent.execution.offer import ExecutionSetup
    from meshforge.agent.settings import AgentSettings

__all__ = ["MAX_ENVS", "MAX_IMAGES", "PrepareService"]

log = get_logger(__name__)

MAX_IMAGES: Final = 8
MAX_ENVS: Final = 32
_REQUEST_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PYTHON_RE: Final = re.compile(r"3\.\d{1,2}")
_RUNTIMES: Final = ("docker", "venv", "subprocess")
_REMEMBERED: Final = 64
_DETAIL_CHARS: Final = 300


class _Preparer(Protocol):
    async def prepare_env(
        self, spec: RunSpec, on_output: Callable[[Stream, bytes], Awaitable[None]]
    ) -> tuple[str, str]: ...


@dataclass(slots=True)
class _Item:
    kind: str
    name: str
    state: str = "pending"
    failure_type: str = ""
    reason: str = ""
    detail: str = ""
    key: str = ""
    size_bytes: int = 0
    duration_s: float = 0.0

    def proto(self) -> agent_pb2.PrepareItem:
        return agent_pb2.PrepareItem(
            kind=self.kind,
            name=self.name,
            state=self.state,
            failure_type=self.failure_type,
            reason=self.reason,
            detail=self.detail[:_DETAIL_CHARS],
            key=self.key,
            size_bytes=self.size_bytes,
            duration_s=self.duration_s,
        )

    def fail(self, failure: FailureType, reason: str, detail: str) -> None:
        self.state = "failed"
        self.failure_type = failure.value
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class _Request:
    request_id: str
    images: list[str]
    envs: list[agent_pb2.EnvSpecRequest]
    items: list[_Item] = field(default_factory=list)
    done: bool = False


class PrepareService:
    """See module docstring. One per agent process."""

    def __init__(
        self,
        settings: AgentSettings,
        setup: ExecutionSetup,
        send_report: Callable[[str, Any], Awaitable[str]],
        *,
        node_id: str = "",
        prepare_images: Callable[[Sequence[str]], Awaitable[PrepareReport]]
        | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._setup = setup
        self._send = send_report
        self._node_id = node_id
        self._prepare_images = prepare_images or (
            lambda bases: prepare(settings, bases=bases)
        )
        self._clock = clock
        self._requests: OrderedDict[str, _Request] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        """One request at a time: two builds of one env would only wait on its
        lock anyway, and image pulls compete for the same link."""
        self.root = settings.data_dir / "prepare"

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------- commands

    def handle(self, msg: agent_pb2.PrepareEnv) -> None:
        """A ``PrepareEnv`` command (synchronous, before its Ack)."""
        request_id = msg.request_id
        if not _REQUEST_ID_RE.fullmatch(request_id):
            log.warning("agent.prepare.refused", node_id=self._node_id, reason="bad_id")
            return
        known = self._requests.get(request_id)
        if known is not None:
            # A replayed command: never run twice; say where it stands.
            self._spawn(self._report(known), f"agent-prepare-status:{request_id}")
            return
        request = _Request(request_id, list(msg.base_images), list(msg.envs))
        self._requests[request_id] = request
        while len(self._requests) > _REMEMBERED:
            self._requests.popitem(last=False)
        self._spawn(self._run(request), f"agent-prepare:{request_id}")

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "agent.prepare.task_failed",
                node_id=self._node_id,
                cause=type(task.exception()).__name__,
            )

    async def _report(self, request: _Request) -> None:
        status = agent_pb2.PrepareEnvStatus(
            request_id=request.request_id,
            done=request.done,
            items=[item.proto() for item in request.items],
        )
        await self._send("prepare_env_status", status)

    # ------------------------------------------------------------------ run

    def _validate(self, request: _Request) -> str | None:
        if len(request.images) > MAX_IMAGES or len(request.envs) > MAX_ENVS:
            return f"at most {MAX_IMAGES} images and {MAX_ENVS} environments"
        if any(name not in BASES for name in request.images):
            return f"unknown base image (known: {', '.join(BASES)})"
        limit = self._settings.agent.envs.max_requirements_bytes
        for env in request.envs:
            if env.base not in BASES:
                return "an environment names an unknown base"
            if env.python and not _PYTHON_RE.fullmatch(env.python):
                return 'an environment python must look like "3.12"'
            if env.runtime and env.runtime not in _RUNTIMES:
                return "an environment names an unknown runtime"
            if len(env.requirements) > limit:
                return f"a requirements file is over {limit} bytes"
        return None

    async def _run(self, request: _Request) -> None:
        refused = self._validate(request)
        request.items = [_Item("image", name) for name in dict.fromkeys(request.images)]
        request.items += [_Item("env", str(i)) for i in range(len(request.envs))]
        if refused is not None:
            for item in request.items:
                item.fail(FailureType.INVALID_INPUT, "invalid_request", refused)
            if not request.items:
                request.items = [_Item("request", "")]
                request.items[0].fail(
                    FailureType.INVALID_INPUT, "invalid_request", refused
                )
            request.done = True
            await self._report(request)
            return
        await self._report(request)  # accepted: every item pending
        async with self._lock:
            images = [i for i in request.items if i.kind == "image"]
            if images:
                await self._images(images)
                await self._report(request)
            for index, env in enumerate(request.envs):
                item = request.items[len(images) + index]
                await self._env(request, item, env)
                await self._report(request)
        request.done = True
        await self._report(request)
        log.info(
            "agent.prepare.done",
            node_id=self._node_id,
            request_id=request.request_id,
            states=[i.state for i in request.items],
        )

    async def _images(self, items: list[_Item]) -> None:
        started = self._clock()
        for item in items:
            item.state = "running"
        report = await self._prepare_images([i.name for i in items])
        by_base = {image.base: image for image in report.images}
        for item in items:
            image = by_base.get(item.name)
            if image is None:
                item.fail(
                    FailureType.ENV_ERROR,
                    "docker_unavailable",
                    f"docker {report.docker}",
                )
                item.duration_s = self._clock() - started
                continue
            item.key = image.image.partition("@")[2]
            item.size_bytes = image.size_bytes or 0
            item.duration_s = image.seconds
            if image.status == "present":
                item.state = "hit"
            elif image.status == "pulled":
                item.state = "pulled"
            else:
                timed_out = "timed out" in image.reason
                item.fail(
                    FailureType.TIMEOUT if timed_out else FailureType.NETWORK_ERROR,
                    "pull_timeout" if timed_out else "pull_failed",
                    image.reason,
                )

    def _runtime(self, wanted: str, base: str) -> tuple[str, _Preparer] | None:
        order = (wanted,) if wanted else _RUNTIMES
        for name in order:
            if name == "venv" and base != "python-cpu":
                continue  # the venv runtime provides python-cpu only (G2)
            runtime = self._setup.runtimes.get(name)
            if runtime is not None and hasattr(runtime, "prepare_env"):
                return name, runtime  # type: ignore[return-value]
        return None

    async def _env(
        self, request: _Request, item: _Item, env: agent_pb2.EnvSpecRequest
    ) -> None:
        started = self._clock()
        item.state = "running"
        try:
            normalised = normalize_requirements(
                env.requirements,
                max_bytes=self._settings.agent.envs.max_requirements_bytes,
                max_lines=self._settings.agent.envs.max_requirements_lines,
                wheels_only=self._settings.agent.envs.only_binary,
            )
        except EnvError as exc:
            item.fail(exc.failure_type, exc.reason, exc.message)
            return
        chosen = self._runtime(env.runtime, env.base)
        if chosen is None:
            item.fail(
                FailureType.NO_FEASIBLE_PLAN,
                "runtime_unavailable",
                f"no {env.runtime or 'usable'} runtime on this node for {env.base}",
            )
            return
        name, runtime = chosen
        attempt_id = str(new_attempt_id())
        workdir = self.root / attempt_id
        try:
            await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                (workdir / "requirements.txt").write_bytes, normalised
            )
            images = self._settings.agent.execution.images
            spec = RunSpec(
                attempt_id=attempt_id,
                argv=("true",),
                workdir=workdir,
                env={},
                limits=RunLimits(
                    memory_bytes=512 * 1024 * 1024, pids=256, timeout_s=60.0
                ),
                image=(
                    images.python_cpu
                    if env.base == "python-cpu"
                    else images.cuda_pytorch
                )
                if name == "docker"
                else None,
                env_request=EnvRequest(
                    base=env.base,
                    python=env.python or None,
                    requirements="requirements.txt",
                ),
            )

            async def discard(_stream: Stream, _data: bytes) -> None:
                return None

            env_hash, cache = await runtime.prepare_env(spec, discard)
        except EnvError as exc:
            item.fail(exc.failure_type, exc.reason, exc.message)
        else:
            item.key = env_hash
            item.state = "hit" if cache == "hit" else "built"
        finally:
            item.duration_s = self._clock() - started
            with contextlib.suppress(OSError):
                await asyncio.to_thread(shutil.rmtree, workdir, True)
        log.info(
            "agent.prepare.env",
            node_id=self._node_id,
            request_id=request.request_id,
            runtime=name,
            env_hash=item.key or None,
            state=item.state,
            reason=item.reason or None,
        )
