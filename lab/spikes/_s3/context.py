"""State every S3 sender shares: limits, counters, the transfer log, liveness.

One :class:`ServerContext` is created by :class:`~spikes._s3.server.S3Server` and
handed to the raw-TCP, HTTP and gRPC senders, so all three obey the same safety
rules (docs/LAB.md §6): LAN peers only, bounded request sizes, bounded idle and
total lifetime, and a single stop flag that unblocks every loop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from spikes._s3.meters import TransferLog

MAX_ERRORS: Final = 100


@dataclass(frozen=True, slots=True)
class ServerLimits:
    duration_s: float = 3600.0
    idle_s: float = 900.0
    #: open connections per transport (bounds threads and memory)
    max_connections: int = 16
    #: a new connection must deliver its complete request header within this time
    header_timeout_s: float = 5.0
    #: per socket operation, not per transfer
    io_timeout_s: float = 30.0
    #: how long ``stats`` waits for a transfer's parts to be recorded
    result_wait_s: float = 5.0


@dataclass(slots=True)
class ServerCounters:
    accepted: int = 0
    refused_peer: int = 0
    refused_busy: int = 0
    header_timeouts: int = 0
    bad_requests: int = 0
    transfers: int = 0
    errors: list[str] = field(default_factory=list)


class ServerContext:
    """Shared, thread-safe server state."""

    def __init__(
        self,
        limits: ServerLimits,
        *,
        name: str,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.limits = limits
        self.name = name
        self.transfers = TransferLog()
        self.counters = ServerCounters()
        self.stopping = threading.Event()
        self._log = log or (lambda _msg: None)
        self._lock = threading.Lock()
        self._last_activity = time.monotonic()

    def say(self, text: str) -> None:
        self._log(text)

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity

    def note_error(self, text: str) -> None:
        with self._lock:
            if len(self.counters.errors) < MAX_ERRORS:
                self.counters.errors.append(text)

    def count(self, field_name: str, delta: int = 1) -> None:
        with self._lock:
            setattr(
                self.counters, field_name, getattr(self.counters, field_name) + delta
            )
