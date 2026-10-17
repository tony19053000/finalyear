"""Job and attempt states (ARCHITECTURE §12.1 / §12.2, brief D49).

Values are persisted (``job.state``, ``attempt.state``) and used as ML labels, so
**never rename a value** - add new ones (with a migration widening the CHECK).

:class:`AttemptState` mirrors the proto ``common.v1.AttemptState`` 1:1 by name
(``ATTEMPT_STATE_<NAME>``; parity test in ``tests/unit/test_proto_enum_parity.py``,
conversion only in :mod:`meshforge.common.proto_codec`). :class:`JobState` has no
wire counterpart: jobs never cross the agent protocol, only attempts do.

The *transitions* between these states live in one place, the controller's
``meshforge.controller.jobs.transitions``; this module only names the states.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "ATTEMPT_TERMINAL_STATES",
    "JOB_TERMINAL_STATES",
    "AttemptPhase",
    "AttemptState",
    "JobState",
]


class JobState(StrEnum):
    """§12.1. ``PLANNING`` is a pass-through in V0.4 (placement is manual)."""

    SUBMITTED = "submitted"
    PLANNING = "planning"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class AttemptState(StrEnum):
    """§12.2: one execution of a job (or chunk) on one node."""

    ASSIGNED = "assigned"
    STAGING = "staging"
    ENV_READY = "env_ready"
    RUNNING = "running"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    LOST = "lost"
    """The node vanished; set by the controller, never reported by an agent."""
    FENCED = "fenced"
    """Superseded by a newer attempt epoch; late reports are rejected."""


class AttemptPhase(StrEnum):
    """Phase timings recorded per attempt (ARCHITECTURE §17; V0.4 set)."""

    QUEUE = "queue"
    STAGE = "stage"
    ENV = "env"
    RUN = "run"
    COLLECT = "collect"
    COMPUTE = "compute"
    """V0.9 G1 (D213): the workload's own kernel time, measured by the workload
    with a monotonic clock (benchmarks' ``results/workload.json``); it lies
    INSIDE ``run``, so it is never added to the other phases."""


JOB_TERMINAL_STATES: Final = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)
ATTEMPT_TERMINAL_STATES: Final = frozenset(
    {AttemptState.DONE, AttemptState.FAILED, AttemptState.LOST, AttemptState.FENCED}
)
