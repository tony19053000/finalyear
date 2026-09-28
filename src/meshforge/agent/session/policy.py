"""Reconnect policy (roadmap 0.3.8, ADR-0002 Updates §3): backoff + what a stream
end / a Hello rejection means. Pure and clock-injected; no I/O.

**Backoff** - full jitter: ``sleep = uniform(0, min(cap, base * 2**n))``. ``n``
grows by one per failed cycle and resets to 0 once a stream has lived
``reset_after_s`` (measured with the injected monotonic clock). With the defaults
(0.5 s / 5 s) the ceilings are 0.5, 1, 2, 4, 5, 5, ...; the defaults and the cap's
bound live in :mod:`meshforge.agent.settings` (``RECONNECT_*``): the cap must not
exceed 5 s because V0.3 criterion 2 requires a restored link to be ONLINE again
within 10 s (D27; criterion 3 alone would allow 15 s).

**Stream end -> action** (:func:`stream_end_action`)::

    OK                                    -> RECONNECT  (controller_shutdown / agent_closed)
    ABORTED "superseded"                  -> STOP_SUPERSEDED  (exit CONFLICT)
    ABORTED "lease_expired"/"protocol_error"/other -> RECONNECT (reason logged)
    UNAVAILABLE / DEADLINE_EXCEEDED / CANCELLED / UNKNOWN / INTERNAL / ... -> RECONNECT
    INVALID_ARGUMENT                      -> FATAL_CONFIG (our own Hello is malformed)
    FAILED_PRECONDITION                   -> RECONNECT (Hello too slow: transient)
    PERMISSION_DENIED                     -> FATAL_CONFIG (node revoked; V0.7 pairing)

**HelloAck{accepted=false} -> action** (:func:`hello_reject_action`)::

    STALE_EPOCH  -> RETRY_NOW while stale_retries < 3, then BACKOFF
    PROTOCOL_VERSION -> FATAL_CONFIG
    UNAVAILABLE (admission missed its deadline, V0.4 D41) -> BACKOFF
    INTERNAL / DUPLICATE_SESSION / UNSPECIFIED / unknown -> BACKOFF
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import grpc

from meshforge.agent.settings import (
    RECONNECT_BASE_S,
    RECONNECT_CAP_S,
    RECONNECT_RESET_AFTER_S,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "MAX_STALE_EPOCH_RETRIES",
    "SUPERSEDED_DETAIL",
    "Backoff",
    "BackoffPolicy",
    "HelloRejectAction",
    "StreamEndAction",
    "hello_reject_action",
    "stream_end_action",
]

SUPERSEDED_DETAIL: Final = "superseded"
"""``ABORTED`` detail the controller sends when a newer epoch replaced the stream."""

MAX_STALE_EPOCH_RETRIES: Final = 3
"""Immediate epoch bump + retry attempts per cycle before falling back to BACKOFF."""

_FATAL_CODES: Final[frozenset[grpc.StatusCode]] = frozenset(
    {grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.PERMISSION_DENIED}
)


class StreamEndAction(StrEnum):
    RECONNECT = "reconnect"
    STOP_SUPERSEDED = "stop_superseded"
    FATAL_CONFIG = "fatal_config"


class HelloRejectAction(StrEnum):
    RETRY_NOW = "retry_now"
    BACKOFF = "backoff"
    FATAL_CONFIG = "fatal_config"


def stream_end_action(
    code: grpc.StatusCode | None, details: str | None
) -> StreamEndAction:
    """Map the final gRPC status of a Session stream to what the agent does next.

    ``code`` is ``None`` when the stream ended without a status (channel torn down
    before a status arrived) - treated as a transient failure.
    """
    if code is grpc.StatusCode.ABORTED:
        if (details or "").strip() == SUPERSEDED_DETAIL:
            return StreamEndAction.STOP_SUPERSEDED
        return StreamEndAction.RECONNECT
    if code in _FATAL_CODES:
        return StreamEndAction.FATAL_CONFIG
    return StreamEndAction.RECONNECT


def hello_reject_action(
    reject_code: agent_pb2.HelloAck.RejectCode.ValueType, stale_retries: int
) -> HelloRejectAction:
    """What to do about ``HelloAck{accepted=false, reject_code}``.

    ``stale_retries`` is how many immediate STALE_EPOCH retries this cycle has
    already made; bounded so a controller that keeps saying "stale" (a persisted
    high-water mark far ahead of us) cannot make us hammer it.
    """
    codes = agent_pb2.HelloAck.RejectCode
    if reject_code == codes.REJECT_CODE_STALE_EPOCH:
        if stale_retries < MAX_STALE_EPOCH_RETRIES:
            return HelloRejectAction.RETRY_NOW
        return HelloRejectAction.BACKOFF
    if reject_code == codes.REJECT_CODE_PROTOCOL_VERSION:
        return HelloRejectAction.FATAL_CONFIG
    # UNAVAILABLE (D41: the controller's database is slow) is transient by
    # definition; like every other code it is retried after a backoff - an
    # immediate retry would only queue behind the same slow admission.
    return HelloRejectAction.BACKOFF


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_s: float = RECONNECT_BASE_S
    cap_s: float = RECONNECT_CAP_S
    reset_after_s: float = RECONNECT_RESET_AFTER_S

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError("base_s must be > 0")
        if self.cap_s < self.base_s:
            raise ValueError("cap_s must be >= base_s")
        if self.reset_after_s < 0:
            raise ValueError("reset_after_s must be >= 0")

    def ceiling(self, attempt: int) -> float:
        """``min(cap, base * 2**attempt)`` - the upper bound of the ``attempt``-th sleep."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        # 2**attempt overflows nothing in Python, but cap it early to keep it cheap.
        if attempt > 60:
            return self.cap_s
        return float(min(self.cap_s, self.base_s * float(2**attempt)))


class Backoff:
    """Stateful full-jitter backoff on an injected clock and random source.

    ``clock`` is monotonic (``loop.time()`` in production); ``rand`` returns a float
    in ``[0, 1)`` (``random.random`` in production, a stub in tests).
    """

    def __init__(
        self,
        policy: BackoffPolicy,
        *,
        clock: Callable[[], float],
        rand: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._rand: Callable[[], float] = rand if rand is not None else random.random
        self.attempt = 0
        """Consecutive failed cycles since the last reset (the exponent ``n``)."""
        self._stream_started_at: float | None = None

    @property
    def policy(self) -> BackoffPolicy:
        return self._policy

    def next_delay(self) -> float:
        """The sleep for the current failure; advances ``attempt``."""
        ceiling = self._policy.ceiling(self.attempt)
        self.attempt += 1
        return self._rand() * ceiling

    def stream_started(self) -> None:
        """A stream was accepted (HelloAck accepted); starts the lifetime clock."""
        self._stream_started_at = self._clock()

    def stream_ended(self) -> float:
        """The accepted stream ended; returns its lifetime and resets ``attempt``
        when it lived at least ``reset_after_s``."""
        if self._stream_started_at is None:
            return 0.0
        lived = max(self._clock() - self._stream_started_at, 0.0)
        self._stream_started_at = None
        if lived >= self._policy.reset_after_s:
            self.attempt = 0
        return lived

    def reset(self) -> None:
        self.attempt = 0
        self._stream_started_at = None
