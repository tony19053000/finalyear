"""Error base classes and the typed failure taxonomy (ARCHITECTURE §13).

Every failure that can end an attempt, a job, a session or a process carries a
:class:`FailureType`. The enum *values* are stable strings: they are stored in the
database, sent over gRPC/REST, used as retry-policy keys and as ML labels, so
**never rename a value** — add new ones instead.

:class:`MeshForgeError` is the base of all MeshForge exceptions. It carries

* ``failure_type`` — stable, typed category (class default, overridable per instance);
* ``retryable`` — whether a retry *may* help (default from :data:`DEFAULT_RETRYABLE`;
  the retry policy still decides max attempts/backoff);
* ``details`` — small, JSON-safe, **non-secret** context for logs/APIs;
* ``exit_code`` — what an entry point should exit with if this error escapes.

Process exit codes stay in :mod:`meshforge.common.exit_codes` (its import path is
kept) and are re-exported here for convenience.

Exceptions that represent bad input also subclass :class:`ValueError`, so Pydantic
validators that raise them produce ordinary ``ValidationError`` entries.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from meshforge.common.exit_codes import ExitCode

__all__ = [
    "DEFAULT_RETRYABLE",
    "ConfigError",
    "DigestMismatchError",
    "ExitCode",
    "FailureType",
    "IdentityError",
    "InvalidIdError",
    "MeshForgeError",
    "is_retryable",
]


class FailureType(StrEnum):
    """Typed failure categories. Names follow ARCHITECTURE §13 where it defines them."""

    # --- §13 attempt/job failures -----------------------------------------------------
    USER_ERROR = "user_error"
    """Workload process crashed or exited non-zero."""
    OOM = "oom"
    """Host RAM exhausted (GPU memory is ``VRAM_OOM``)."""
    VRAM_OOM = "vram_oom"
    """GPU memory (VRAM) exhausted; the planner may retry on a node with more VRAM."""
    ENV_ERROR = "env_error"
    """Execution environment (image/venv) could not be built."""
    DISK_FULL = "disk_full"
    """Not enough disk for staging, environments or results."""
    NODE_LOST = "node_lost"
    """Node vanished (lease expired / stream lost); attempt state becomes ``LOST``."""
    GPU_SESSION_LOST = "gpu_session_lost"
    """Remote/combined GPU session dropped."""
    TRANSFER_ERROR = "transfer_error"
    """Blob/data transfer failed or was interrupted."""
    INTEGRITY_ERROR = "integrity_error"
    """Content did not match its SHA-256 digest (corrupted transfer or storage)."""
    FENCED = "fenced"
    """Report from a superseded attempt (stale fencing token); rejected."""
    CANCELLED = "cancelled"
    """Cancelled by a user or by the controller."""
    TIMEOUT = "timeout"
    """An infrastructure operation timed out (RPC, transfer, env build step…);
    a retry may succeed. Not for workload limits — see ``DEADLINE_EXCEEDED``."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The workload hit its own configured time limit; retrying the same work
    would hit it again, so not retryable by default."""
    RESOURCE_BUSY = "resource_busy"
    """Target node/GPU is temporarily busy or at capacity (slots, VRAM reserved,
    session limit); retry later or elsewhere."""
    NO_FEASIBLE_PLAN = "no_feasible_plan"
    """Planner found no node/mode able to run the workload."""
    # --- infrastructure / programming errors ------------------------------------------
    NETWORK_ERROR = "network_error"
    """Transient connectivity problem (RPC unavailable, connection reset…)."""
    CONFIG_ERROR = "config_error"
    """Invalid or missing configuration."""
    INVALID_INPUT = "invalid_input"
    """Malformed request/ID/manifest supplied by a caller."""
    UNAUTHORIZED = "unauthorized"
    """Authentication or authorization failed."""
    NOT_FOUND = "not_found"
    """Referenced entity does not exist."""
    CONFLICT = "conflict"
    """State conflict (e.g. illegal state transition, duplicate)."""
    INTERNAL_ERROR = "internal_error"
    """Bug or unexpected condition inside MeshForge."""
    # --- added later (appended: proto numbers follow declaration order) ---------------
    OUTPUT_ERROR = "output_error"
    """The workload finished but its declared outputs could not be collected or
    uploaded (V0.4 D74: a refused link/escape, over the quota, an upload failure).
    The failure's own ``retryable`` flag says whether a retry may help."""
    OWNER_PREEMPTED = "owner_preempted"
    """V0.9 G5 (D219/D246): a **benchmark** attempt was stopped (or refused at
    start) because the node's owner paused benchmarks or is using the laptop.
    Never a user job. Not retried by the job; the campaign runner requeues the
    run without spending an infrastructure retry."""


DEFAULT_RETRYABLE: Mapping[FailureType, bool] = {
    FailureType.USER_ERROR: False,  # §13: not retried by default
    FailureType.OOM: False,  # §13: optional retry on a larger node — planner decides
    FailureType.VRAM_OOM: True,  # planner may pick a node with more free VRAM
    FailureType.ENV_ERROR: False,  # cached as known-bad for that env hash
    FailureType.DISK_FULL: False,  # GC first; planner may pick another node
    FailureType.NODE_LOST: True,
    FailureType.GPU_SESSION_LOST: True,
    FailureType.TRANSFER_ERROR: True,
    FailureType.INTEGRITY_ERROR: True,  # re-fetch from another holder
    FailureType.FENCED: False,
    FailureType.CANCELLED: False,
    FailureType.TIMEOUT: True,
    FailureType.DEADLINE_EXCEEDED: False,
    FailureType.RESOURCE_BUSY: True,
    FailureType.NO_FEASIBLE_PLAN: False,
    FailureType.OUTPUT_ERROR: False,  # per instance: an upload blip is retryable
    FailureType.NETWORK_ERROR: True,
    FailureType.CONFIG_ERROR: False,
    FailureType.INVALID_INPUT: False,
    FailureType.UNAUTHORIZED: False,
    FailureType.NOT_FOUND: False,
    FailureType.CONFLICT: False,
    FailureType.INTERNAL_ERROR: False,
    FailureType.OWNER_PREEMPTED: False,  # the campaign runner requeues the run
}


def is_retryable(failure_type: FailureType) -> bool:
    """Default retryability of a failure type."""
    return DEFAULT_RETRYABLE[failure_type]


class MeshForgeError(Exception):
    """Base class for all MeshForge errors."""

    default_failure_type: ClassVar[FailureType] = FailureType.INTERNAL_ERROR
    exit_code: ClassVar[ExitCode] = ExitCode.FAILURE

    def __init__(
        self,
        message: str,
        *,
        failure_type: FailureType | None = None,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.failure_type: FailureType = failure_type or self.default_failure_type
        self.retryable: bool = (
            retryable if retryable is not None else is_retryable(self.failure_type)
        )
        self.details: dict[str, Any] = dict(details or {})

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({self.message!r}, failure_type={self.failure_type.value!r}, "
            f"retryable={self.retryable!r})"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        # Keyword-only arguments break default exception pickling (multiprocessing,
        # executors); rebuild explicitly.
        return (
            _rebuild_error,
            (type(self), self.message, self.failure_type, self.retryable, self.details),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation for logs, telemetry and API error bodies."""
        return {
            "error": type(self).__name__,
            "failure_type": self.failure_type.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


def _rebuild_error(
    cls: type[MeshForgeError],
    message: str,
    failure_type: FailureType,
    retryable: bool,
    details: dict[str, Any],
) -> MeshForgeError:
    err = cls.__new__(cls)
    MeshForgeError.__init__(
        err, message, failure_type=failure_type, retryable=retryable, details=details
    )
    return err


class ConfigError(MeshForgeError, ValueError):
    """Configuration file/env/arguments could not be loaded or are invalid."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


class InvalidIdError(MeshForgeError, ValueError):
    """A string is not a valid MeshForge ID of the expected kind."""

    default_failure_type = FailureType.INVALID_INPUT


class DigestMismatchError(MeshForgeError):
    """Content does not match its expected SHA-256 digest."""

    default_failure_type = FailureType.INTEGRITY_ERROR


class IdentityError(MeshForgeError):
    """The node identity on disk is missing in part, corrupt or unprotectable (ADR-0013).

    Fatal by design: the agent must never mint a new ``node_id`` next to an old
    key or run with a key it could not protect. ``path`` is the offending file or
    directory and ``remedy`` tells the operator what to do; both appear in the
    message and in ``details`` (never any key material).
    """

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR

    def __init__(self, problem: str, path: Path, remedy: str) -> None:
        super().__init__(
            f"{problem} ({path}) - {remedy}",
            details={"path": str(path), "remedy": remedy},
        )
        self.problem = problem
        self.path = path
        self.remedy = remedy

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.problem, self.path, self.remedy))
