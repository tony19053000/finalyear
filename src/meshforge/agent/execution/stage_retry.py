"""The staging retry window (V0.5 D95, V1.0 G9-A D429).

A staging try that fails with a transient error (``transfer_error`` /
``timeout`` - the controller or the link went away - or ``unauthorized``, the
controller refused an old fetch token) is tried again. How long:
``[agent.execution] staging_retry_s`` is **how long staging may keep failing
without making progress**:

* the window opens when staging starts;
* a failed try that made **durable progress**
  (:class:`~meshforge.agent.cas.progress.StagingProgress`: a chunk verified, a
  blob installed, a manifest stored, a combine part sealed) restarts the window
  at the time of that failure and resets the backoff - the next try resumes
  that work, so the staging is getting somewhere;
* a failed try with no progress leaves the window where it is; once the
  window has passed (checked after the failure and again before every retry, a
  refreshed token included) the staging fails ``transfer_error`` naming the
  last cause;
* ``staging_retry_s = 0``: no retry, the first error is reported as is.

Before D429 the window counted from the staging start, so a 5 GB fetch over
Wi-Fi (300-480 s) that met one transient error after the first minute failed
at once although it had verified gigabytes (lab run 2026-11-04).

The backoff between tries is 0.25 s doubling to 5 s; a controller refusal
waits for ``RefreshFetchToken`` instead (the caller decides, this class only
keeps the clock). Time is the injected monotonic clock only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from meshforge.agent.cas.progress import StagingProgress
from meshforge.agent.cas.sources import FetchError
from meshforge.common.errors import FailureType

__all__ = [
    "STAGE_RETRYABLE",
    "STAGE_RETRY_BASE_S",
    "STAGE_RETRY_CAP_S",
    "StageRetryWindow",
    "controller_refused",
]

STAGE_RETRYABLE: Final = frozenset(
    {FailureType.TRANSFER_ERROR, FailureType.TIMEOUT, FailureType.UNAUTHORIZED}
)
"""Staging failures a lost link or a controller restart causes (connection
refused/reset, a body cut off, a timeout, the old fetch token refused)."""
STAGE_RETRY_BASE_S: Final = 0.25
STAGE_RETRY_CAP_S: Final = 5.0


def controller_refused(error: FetchError) -> bool:
    """A 401/403 from the controller (a bundle fetch names no source: it is
    always the controller). Only this waits for RefreshFetchToken (D95)."""
    return error.failure_type is FailureType.UNAUTHORIZED and error.source in (
        "",
        "controller",
    )


class StageRetryWindow:
    """See module docstring. One per staging (one retry loop)."""

    def __init__(
        self,
        budget_s: float,
        clock: Callable[[], float],
        progress: StagingProgress,
    ) -> None:
        self._budget = budget_s
        self._clock = clock
        self._progress = progress
        self._seen = progress.units
        self.deadline = clock() + budget_s
        """No staging try starts at or after this (monotonic clock)."""
        self._delay = STAGE_RETRY_BASE_S

    @property
    def budget_s(self) -> float:
        return self._budget

    @property
    def retries(self) -> bool:
        """False = ``staging_retry_s = 0``: the first error is final."""
        return self._budget > 0

    def failed(self) -> bool:
        """Note one failed try; True = it made durable progress since the
        last failure (or the start), so the window restarts now and the
        backoff starts again from its base."""
        units = self._progress.units
        if units == self._seen:
            return False
        self._seen = units
        self.deadline = self._clock() + self._budget
        self._delay = STAGE_RETRY_BASE_S
        return True

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def backoff(self) -> float:
        """The next wait before a retry (never past the window), doubling."""
        wait = max(0.0, min(self._delay, self.remaining()))
        self._delay = min(self._delay * 2, STAGE_RETRY_CAP_S)
        return wait

    def check(self, last: FetchError) -> None:
        """Raise ``transfer_error`` naming ``last`` once the window passed."""
        if self._clock() >= self.deadline:
            raise FetchError(
                f"staging did not succeed within {self._budget:g} s without "
                f"progress (last: {last.message})",
                failure_type=FailureType.TRANSFER_ERROR,
            )
