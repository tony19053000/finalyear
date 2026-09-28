"""The execution ``Runtime`` contract (brief D51, ADR-0004, roadmap 0.4.7).

A runtime runs **one attempt's workload** in a prepared host directory and reports
how it ended. It knows nothing about bundles, the controller or the session: G4's
executor fetches and unpacks the job, builds a :class:`RunSpec`, picks a runtime and
turns the :class:`RunResult` into an ``AttemptStatus``. Two implementations share
this contract and one parametrised test module:

* :class:`~meshforge.agent.execution.docker_runtime.DockerRuntime` - the sandbox;
* :class:`~meshforge.agent.execution.subprocess_runtime.SubprocessRuntime` - the
  **unsandboxed** fallback (``[agent.execution] allow_unsandboxed``).

**The contract.**

* ``run()`` never raises for a workload or infrastructure problem: every ending is a
  :class:`RunResult` with a typed ``failure_type`` (``None`` = exit 0) and, for
  infrastructure faults, a stable ``reason`` code. Mapping (ARCHITECTURE §13):
  non-zero exit or death by signal -> ``user_error``; the workload's own time limit
  -> ``deadline_exceeded`` (enforced *inside* ``run()``, within 2 s of the limit);
  memory limit -> ``oom``; image/requirements problems -> ``env_error``; the env
  phase outliving ``env_timeout_s`` -> ``timeout``; a runtime fault ->
  ``internal_error``.
* **Cancellation = cancelling the task that runs ``run()``.** The runtime asks the
  workload to stop (SIGTERM), kills it after ``grace_s``, removes what it created,
  and only then lets the plain ``asyncio.CancelledError`` propagate - within
  ``grace_s + 2 s``. The exception is *not* swallowed or replaced (``asyncio.timeout``
  and task groups rely on that), so a caller that wants the partial facts passes a
  :class:`RunObserver`: the runtime keeps it current, and
  :meth:`RunObserver.cancelled_result` then builds the ``cancelled`` ``RunResult``
  (phase timings included).
* ``on_output(stream, data)`` receives the workload's stdout/stderr **bytes as they
  arrive** (no decoding, no line splitting - invalid UTF-8 and binary pass through),
  in order per stream, from **one** task (never two calls at once). It applies
  back-pressure: a slow consumer slows the workload's writes, it never buffers
  without bound. It must not raise; if it does, the run is stopped and ends
  ``internal_error`` (``reason = output_consumer_failed``).
* Environment: exactly ``RunSpec.env`` (plus what the runtime documents it adds,
  e.g. ``HOME``/``PYTHONPATH``) - never the agent's own environment.
* Phase durations come from the monotonic clock: ``env_s`` (image pull + setup
  container / requirements install) and ``run_s`` (workload start to exit).
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from meshforge.common.errors import FailureType
from meshforge.common.ids import validate_attempt_id

__all__ = [
    "DEFAULT_GRACE_S",
    "DEPS_SUBDIR",
    "MAX_ARGV_ITEMS",
    "MAX_ARG_LENGTH",
    "MAX_ENV_ITEMS",
    "MAX_ENV_VALUE_LENGTH",
    "MAX_GPU_INDEX",
    "OutputCallback",
    "Phase",
    "RunLimits",
    "RunObserver",
    "RunResult",
    "RunSpec",
    "Runtime",
    "RuntimeAvailability",
    "Stream",
    "classify",
]

DEFAULT_GRACE_S: Final = 3.0
DEPS_SUBDIR: Final = ".meshforge/deps"
"""Where a setup step installs requirements, relative to the workdir (brief D51);
the run step gets it on ``PYTHONPATH``. V0.5 caches it by env hash."""

MAX_ARGV_ITEMS: Final = 4096
MAX_ARG_LENGTH: Final = 32_768
MAX_ENV_ITEMS: Final = 1024
MAX_ENV_VALUE_LENGTH: Final = 32_768
MAX_GPU_INDEX: Final = 1023
_DETAIL_MAX: Final = 500
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class Stream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class Phase(StrEnum):
    """Where a run is; kept current in a :class:`RunObserver`."""

    PENDING = "pending"
    ENV = "env"
    RUN = "run"
    STOPPING = "stopping"
    DONE = "done"


OutputCallback = Callable[[Stream, bytes], Awaitable[None]]


def _check_argv(name: str, argv: tuple[str, ...]) -> None:
    if not argv or not argv[0]:
        raise ValueError(f"{name} must not be empty")
    if len(argv) > MAX_ARGV_ITEMS:
        raise ValueError(
            f"{name} has {len(argv)} items; the maximum is {MAX_ARGV_ITEMS}"
        )
    for item in argv:
        if not isinstance(item, str):
            raise TypeError(f"{name} items must be strings")
        if "\x00" in item:
            raise ValueError(f"{name} items must not contain NUL")
        if len(item) > MAX_ARG_LENGTH:
            raise ValueError(f"a {name} item is over {MAX_ARG_LENGTH} characters")


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Resource limits for one run. ``cpus`` None = no CPU limit."""

    memory_bytes: int
    pids: int
    timeout_s: float
    cpus: float | None = None

    def __post_init__(self) -> None:
        if self.memory_bytes < 6 * 1024 * 1024:
            raise ValueError("memory_bytes must be >= 6 MiB (Docker's minimum)")
        if self.pids < 1:
            raise ValueError("pids must be >= 1")
        if not self.timeout_s > 0:
            raise ValueError("timeout_s must be > 0")
        if self.cpus is not None and not self.cpus > 0:
            raise ValueError("cpus must be > 0 when set")


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Everything a runtime needs for one attempt (brief D51). Validated on creation.

    ``workdir`` is an absolute host directory that exists when ``run()`` starts;
    the Docker runtime mounts it (and nothing else) at ``/work``. ``env`` is the
    workload's **whole** environment: an explicit allowlist plus the manifest's
    variables, built by the executor - never ``os.environ``. ``image`` is required
    by the Docker runtime and ignored by the subprocess runtime. ``gpu_indices``
    are NVML indices (= ``HardwareSummary.Gpu.index``); empty = no GPU at all.
    ``setup_argv`` (e.g. a ``pip install --target <workdir>/.meshforge/deps``) runs
    first with the network on; its duration is the ``env`` phase.
    """

    attempt_id: str
    argv: tuple[str, ...]
    workdir: Path
    env: Mapping[str, str]
    limits: RunLimits
    image: str | None = None
    setup_argv: tuple[str, ...] | None = None
    gpu_indices: tuple[int, ...] = ()
    network: bool = False
    grace_s: float = DEFAULT_GRACE_S

    def __post_init__(self) -> None:
        validate_attempt_id(self.attempt_id)
        object.__setattr__(self, "argv", tuple(self.argv))
        _check_argv("argv", self.argv)
        if self.setup_argv is not None:
            object.__setattr__(self, "setup_argv", tuple(self.setup_argv))
            _check_argv("setup_argv", self.setup_argv)
        if not self.workdir.is_absolute():
            raise ValueError("workdir must be an absolute path")
        env = dict(self.env)
        if len(env) > MAX_ENV_ITEMS:
            raise ValueError(
                f"env has {len(env)} variables; the maximum is {MAX_ENV_ITEMS}"
            )
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
                raise ValueError("env names must match [A-Za-z_][A-Za-z0-9_]{0,127}")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"env {key} must be a string without NUL")
            if len(value) > MAX_ENV_VALUE_LENGTH:
                raise ValueError(f"env {key} is over {MAX_ENV_VALUE_LENGTH} characters")
        object.__setattr__(self, "env", MappingProxyType(env))
        indices = tuple(self.gpu_indices)
        if any(not isinstance(i, int) or not 0 <= i <= MAX_GPU_INDEX for i in indices):
            raise ValueError(
                f"gpu_indices must be NVML indices in [0, {MAX_GPU_INDEX}]"
            )
        if len(set(indices)) != len(indices):
            raise ValueError("gpu_indices must not repeat")
        object.__setattr__(self, "gpu_indices", indices)
        if not 0 < self.grace_s <= 30:
            raise ValueError("grace_s must be in (0, 30]")
        if self.image is not None and not self.image.strip():
            raise ValueError("image must not be blank")

    @property
    def deps_dir(self) -> Path:
        """Host path of the requirements target (``<workdir>/.meshforge/deps``)."""
        return self.workdir / DEPS_SUBDIR


@dataclass(frozen=True, slots=True)
class RunResult:
    """How one run ended. ``failure_type`` None means exit code 0."""

    exit_code: int | None
    signal: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    oom_killed: bool = False
    failure_type: FailureType | None = None
    reason: str | None = None
    """Stable code for infrastructure outcomes (``image_pull_failed``, ...)."""
    detail: str = ""
    env_s: float = 0.0
    run_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _clip(self.detail))

    @property
    def phases(self) -> dict[str, float]:
        return {"env": self.env_s, "run": self.run_s}

    @property
    def ok(self) -> bool:
        return self.failure_type is None


def _clip(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text)
    return cleaned[:_DETAIL_MAX]


def classify(
    *,
    exit_code: int | None,
    signal: int | None,
    timed_out: bool,
    oom_killed: bool,
) -> FailureType | None:
    """The §13 failure type of a workload that ran (the order is the contract:
    a timeout kill is ``deadline_exceeded`` even though it ends by signal, and an
    OOM kill is ``oom`` even though it looks like a SIGKILL)."""
    if timed_out:
        return FailureType.DEADLINE_EXCEEDED
    if oom_killed:
        return FailureType.OOM
    if exit_code == 0 and signal is None:
        return None
    return FailureType.USER_ERROR


@dataclass(slots=True)
class RunObserver:
    """Live facts about a run, kept current by the runtime (one writer).

    Pass one to ``run()`` to still have the phase timings and the exit facts when
    the run is cancelled (the runtime re-raises ``CancelledError``; see the module
    docstring). ``clock`` must be the one the runtime uses (monotonic).
    """

    clock: Callable[[], float] = time.monotonic
    phase: Phase = Phase.PENDING
    env_started: float | None = None
    env_s: float = 0.0
    run_started: float | None = None
    run_s: float = 0.0
    exit_code: int | None = None
    signal: int | None = None
    ident: str | None = None
    """Container id or process id of the current step (for logs)."""
    extra: dict[str, str] = field(default_factory=dict)

    def enter_env(self) -> None:
        self.phase = Phase.ENV
        self.env_started = self.clock()

    def leave_env(self) -> None:
        if self.env_started is not None:
            self.env_s = max(0.0, self.clock() - self.env_started)

    def enter_run(self) -> None:
        if self.phase is Phase.ENV:
            self.leave_env()
        self.phase = Phase.RUN
        self.run_started = self.clock()

    def leave_run(self) -> None:
        if self.run_started is not None:
            self.run_s = max(0.0, self.clock() - self.run_started)

    def finish(self) -> None:
        if self.phase is Phase.ENV:
            self.leave_env()
        elif self.phase in (Phase.RUN, Phase.STOPPING) and self.run_started is not None:
            self.leave_run()
        self.phase = Phase.DONE

    def cancelled_result(self, detail: str = "cancelled") -> RunResult:
        """The ``cancelled`` result of a run whose task was cancelled."""
        return RunResult(
            exit_code=self.exit_code,
            signal=self.signal,
            cancelled=True,
            failure_type=FailureType.CANCELLED,
            detail=detail,
            env_s=self.env_s,
            run_s=self.run_s,
        )


@dataclass(frozen=True, slots=True)
class RuntimeAvailability:
    """What :meth:`Runtime.probe` found. ``reason`` explains a ``False`` in a
    stable, operator-readable way (e.g. ``docker_daemon_unreachable``)."""

    name: str
    available: bool
    reason: str = ""
    gpu_capable: bool = False
    gpu_reason: str = ""
    version: str = ""
    sandboxed: bool = False


@runtime_checkable
class Runtime(Protocol):
    """One way to run a workload (brief D51)."""

    @property
    def name(self) -> str: ...

    async def probe(self) -> RuntimeAvailability: ...

    async def run(
        self,
        spec: RunSpec,
        on_output: OutputCallback,
        *,
        observer: RunObserver | None = None,
    ) -> RunResult: ...
