"""What every condition controller is (V0.9 G4, D217): apply -> verify -> revert.

A controller changes the machine for one attempt and proves the change took:
:meth:`ConditionController.apply` makes it **and verifies it** (measured
values go to :attr:`~ConditionController.measured`); a change that cannot be
made or verified raises :class:`ConditionFailedError` - the caller reverts
whatever was made and fails the attempt ``config_error``
``condition_failed:<kind>: ...`` before anything runs, so an unverified
condition is never labelled (D217). :meth:`~ConditionController.after_stage`
is the second checkpoint for conditions that are only visible once the inputs
are staged (cache). :meth:`~ConditionController.revert` undoes it (idempotent;
False = it could not, the marker keeps it for the next start's sweep), and
:meth:`~ConditionController.broken` says whether the condition stopped holding
while the attempt ran (a load process died) - the report is then
``verified = false`` even though the job finished.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = [
    "CONDITION_FAILED",
    "ConditionController",
    "ConditionFailedError",
    "StageFacts",
    "kill_and_reap",
]

CONDITION_FAILED = "condition_failed"
"""The failure detail prefix (the typed failure is ``config_error``; the run
record's failure class is ``condition_failed``)."""


class ConditionFailedError(MeshForgeError):
    """A condition could not be applied or verified (not retried: the same
    node would fail it again; the campaign runner decides)."""

    default_failure_type: ClassVar[FailureType] = FailureType.CONFIG_ERROR

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(
            f"{CONDITION_FAILED}:{kind}: {reason}",
            retryable=False,
            details={"kind": kind},
        )
        self.kind = kind
        self.reason = reason


class StageFacts:
    """What staging one tree cost, as a condition reads it (the executor's
    ``StageReport`` has these attributes)."""

    blobs_hit: int
    bytes_hit: int
    blobs_fetched: int
    bytes_fetched: int
    manifest_hit: bool


class ConditionController:
    """Base class (see module docstring)."""

    kind: ClassVar[str] = ""

    def __init__(self) -> None:
        self.measured: dict[str, Any] = {}

    def marker_entry(self) -> Mapping[str, Any]:
        """What the start-up sweep needs to undo this (recorded before the
        change and again after it)."""
        return {}

    async def apply(self) -> None:
        """Make and verify the change. Raises :class:`ConditionFailedError`."""

    def after_stage(self, stages: Sequence[Any]) -> None:
        """Check what staging saw. Raises :class:`ConditionFailedError`."""

    def broken(self) -> str | None:
        """Why the condition stopped holding during the attempt (None = held)."""
        return None

    async def revert(self) -> bool:
        """Undo the change (idempotent). False = it could not."""
        return True


async def kill_and_reap(
    proc: asyncio.subprocess.Process, *, timeout_s: float | None = None
) -> bool:
    """Kill ``proc`` and wait until it is reaped (G5 item 0.2, D245 N3): the
    one helper every condition controller that starts a child (``tc``, the
    burners) uses, so a stopped child can never act after its caller moved on.

    A cancellation that arrives while waiting is **held** until the child is
    reaped (a second cancel cannot skip the wait) and then re-raised. With
    ``timeout_s`` the wait gives up after that long (a child stuck in the
    kernel) and returns False; True = the child is gone and reaped."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    reaped = asyncio.ensure_future(proc.wait())
    cancelled = False
    loop = asyncio.get_running_loop()
    deadline = None if timeout_s is None else loop.time() + timeout_s
    while not reaped.done():
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            break
        try:
            await asyncio.wait({reaped}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    if not reaped.done():
        reaped.cancel()
        return False
    return True
