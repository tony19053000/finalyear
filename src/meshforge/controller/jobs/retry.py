"""Whether an ended attempt's job is tried again (V0.6 G3, D110, ADR-0015).

Pure: no I/O, no clock. :class:`~.execution_store.ExecutionStore` calls
:func:`retry_verdict` inside the transaction that ends the attempt and, on
``retry``, moves the job ``RETRYING -> PLANNING -> QUEUED`` with its
``not_before`` **in that same transaction** (ADR-0015 decision 3) - so no
moment exists in which the old attempt has ended and the job is neither
failed nor queued, and nothing a crash could leave half-done.

The rules, in order:

1. ``cancelled`` is never retried (the user, or the controller for the user,
   stopped it); neither is anything once a cancel was requested.
2. A **loss** - the controller ending the attempt ``LOST``, or the agent
   reporting ``node_lost`` (``agent_shutdown``) - is retried unless the job
   opted out with ``retry: {on_lost: false}`` (D117: a non-idempotent
   workload that may have run partly must not run again).
3. Otherwise the failure's own ``retryable`` flag decides (the agent sends
   one per instance - e.g. an ``output_error`` from an upload blip is
   retryable, one from a refused symlink is not); a report without one falls
   back to the type's default (ARCHITECTURE §13 table).
4. A retryable failure of the job's ``max_attempts``-th attempt ends the job
   ``FAILED(<that failure type>)`` with ``retries_exhausted`` in the detail.

Backoff: ``backoff_s * 2^(attempts - 1)``, capped at :data:`MAX_BACKOFF_S`
(``backoff_s = 0`` retries at the next dispatch tick).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from meshforge.common.errors import FailureType, is_retryable

__all__ = [
    "MAX_BACKOFF_S",
    "RetryReason",
    "RetryVerdict",
    "backoff_s",
    "exhausted_detail",
    "retry_verdict",
]

MAX_BACKOFF_S: Final = 300.0
"""D110: the doubling backoff never waits longer than this."""


class RetryReason(StrEnum):
    RETRY = "retry"
    NOT_RETRYABLE = "not_retryable"
    CANCELLED = "cancelled"
    ON_LOST_DISABLED = "on_lost_disabled"
    RETRIES_EXHAUSTED = "retries_exhausted"


@dataclass(frozen=True, slots=True)
class RetryVerdict:
    reason: RetryReason
    backoff_s: float = 0.0
    """Only for ``RETRY``: how long the job waits before it may be placed."""

    @property
    def retry(self) -> bool:
        return self.reason is RetryReason.RETRY


def backoff_s(attempts_used: int, base_s: float) -> float:
    """D110: ``base * 2^(attempts_used - 1)``, capped (attempts_used >= 1)."""
    if base_s <= 0:
        return 0.0
    exponent = min(max(attempts_used, 1) - 1, 16)  # 2^16 * 1 s is past the cap
    return float(min(MAX_BACKOFF_S, base_s * float(2**exponent)))


def retry_verdict(
    *,
    failure_type: FailureType,
    retryable: bool | None,
    lost: bool,
    attempts_used: int,
    max_attempts: int,
    base_backoff_s: float,
    on_lost_retry: bool,
    cancel_requested: bool,
) -> RetryVerdict:
    """See module docstring. ``attempts_used`` = attempts the job has had,
    the one ending now included (= its fencing token)."""
    if failure_type is FailureType.CANCELLED or cancel_requested:
        return RetryVerdict(RetryReason.CANCELLED)
    loss = lost or failure_type is FailureType.NODE_LOST
    if loss:
        if not on_lost_retry:
            return RetryVerdict(RetryReason.ON_LOST_DISABLED)
        may_retry = True
    else:
        may_retry = retryable if retryable is not None else is_retryable(failure_type)
    if not may_retry:
        return RetryVerdict(RetryReason.NOT_RETRYABLE)
    if attempts_used >= max_attempts:
        return RetryVerdict(RetryReason.RETRIES_EXHAUSTED)
    return RetryVerdict(RetryReason.RETRY, backoff_s(attempts_used, base_backoff_s))


def exhausted_detail(attempts_used: int, detail: str | None) -> str:
    """The job's ``failure_detail`` when its last retry failed (D110)."""
    head = f"retries_exhausted after {attempts_used} attempts"
    return f"{head}: {detail}" if detail else head
