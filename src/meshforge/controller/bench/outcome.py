"""What a finished benchmark job means for its run (V0.9 G5, roadmap 0.9.7, D219/D246). Pure.

The campaign runner owns a started campaign; when a run's job ends,
:func:`settle` decides:

* job ``succeeded`` -> the run is ``done``;
* ``owner_preempted`` (the owner guard stopped or refused it) -> **requeued**
  (``planned`` again), ``tries`` unchanged - an owner using their laptop is
  not a fault of the fabric, and it is excluded from the campaign's
  infrastructure failure rate;
* an **infrastructure** failure (:data:`INFRA_FAILURES`: node lost, transfer,
  timeout, env build, disk, internal...) -> requeued with ``tries + 1`` while
  ``tries < max_infra_retries``, else ``failed``;
* anything else -> ``failed``, never retried: the workload's own failure
  (:data:`WORKLOAD_FAILURES` - the same set the run record's failure class
  uses), a condition that could not be verified (``config_error``
  ``condition_failed:``; the same node would fail it again, D217), a cancel,
  an owner policy refusal, a node that can no longer run it.

A campaign that stopped submitting (``finished_reason`` set: budget, cancel)
does not requeue: what would be requeued is ``skipped``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from meshforge.common.errors import FailureType
from meshforge.common.states import JobState

__all__ = [
    "INFRA_FAILURES",
    "WORKLOAD_FAILURES",
    "Settled",
    "counts_as_infra",
    "settle",
]

WORKLOAD_FAILURES: Final = frozenset(
    {
        FailureType.USER_ERROR,
        FailureType.OOM,
        FailureType.VRAM_OOM,
        FailureType.DEADLINE_EXCEEDED,
        FailureType.INVALID_INPUT,
    }
)
"""Copy of ``controller.telemetry.record.WORKLOAD_FAILURES`` (a unit test
holds them equal)."""
NOT_RETRIED: Final = WORKLOAD_FAILURES | frozenset(
    {
        FailureType.CANCELLED,
        FailureType.CONFIG_ERROR,
        FailureType.NO_FEASIBLE_PLAN,
        FailureType.UNAUTHORIZED,
        FailureType.NOT_FOUND,
        FailureType.CONFLICT,
        FailureType.OWNER_PREEMPTED,
    }
)
INFRA_FAILURES: Final = frozenset(FailureType) - NOT_RETRIED
"""What a retry on the same node may fix."""

type RunState = Literal["done", "failed", "planned", "skipped"]


@dataclass(frozen=True, slots=True)
class Settled:
    state: RunState
    tries: int
    last_failure: str | None
    detail: str | None

    @property
    def requeued(self) -> bool:
        return self.state == "planned"


def _type(failure_type: str | None) -> FailureType | None:
    try:
        return FailureType(failure_type) if failure_type else None
    except ValueError:
        return None


def counts_as_infra(failure_type: str | None) -> bool:
    """A failed job of this type counts in the campaign's infra failure rate
    (criterion 1): infrastructure, never ``owner_preempted``."""
    typed = _type(failure_type)
    return typed is None or typed in INFRA_FAILURES


def settle(
    job_state: str,
    failure_type: str | None,
    *,
    tries: int,
    max_infra_retries: int,
    stopped: bool = False,
) -> Settled:
    """See the module docstring. ``stopped`` = the campaign submits no more."""
    if job_state == JobState.SUCCEEDED.value:
        return Settled("done", tries, None, None)
    typed = _type(failure_type)
    label = (failure_type or "unknown")[:64]
    if typed is FailureType.OWNER_PREEMPTED or (
        typed in INFRA_FAILURES and tries < max_infra_retries
    ):
        retry_tries = tries if typed is FailureType.OWNER_PREEMPTED else tries + 1
        if stopped:
            return Settled(
                "skipped", tries, label, f"job {job_state}: {label}; not requeued"
            )
        return Settled(
            "planned", retry_tries, label, f"requeued after job {job_state}: {label}"
        )
    if typed is None and failure_type is None and tries < max_infra_retries:
        # a failed job without a type (should not happen): infra, retried
        if stopped:
            return Settled("skipped", tries, label, f"job {job_state}; not requeued")
        return Settled("planned", tries + 1, label, f"requeued after job {job_state}")
    return Settled("failed", tries, label, f"job {job_state}: {label}")
