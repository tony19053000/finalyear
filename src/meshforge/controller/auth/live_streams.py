"""Long-lived streams lose their user when the token does (V0.7 G4 fix round 1, D157 T-1).

A REST request is authenticated once and finishes; a log WebSocket
(``/v1/jobs/{id}/logs/ws``) is authenticated at its handshake and may then
stay open for hours. RE-S1 says a revoke, an expiry or a disabled user acts on
the *next request* - for a stream that is "within one re-check":

* every authenticated stream registers its API token here
  (:meth:`LiveStreams.open`) and unregisters when it ends;
* :meth:`LiveStreams.run` re-checks **all** registered tokens every
  ``[controller.auth] stream_recheck_interval_s`` (1 s) with **one** query
  (:meth:`~meshforge.controller.auth.users.UserStore.live_token_ids`: not
  revoked, not expired, user enabled - the rule of
  :meth:`~meshforge.controller.auth.users.UserStore.authenticate`). That is
  how a ``meshforge-controller user disable`` (another process, writing the
  database directly) reaches an open stream - the same periodic path the
  revocation sync gives node certificates;
* :meth:`LiveStreams.recheck_now` wakes the loop at once - the notify path: a
  token revoked through this controller's REST API (``compute logout``,
  ``compute tokens revoke``) closes its streams without waiting for the tick;
* a stream whose token is no longer live gets its
  :attr:`StreamAuth.revoked` event set; the stream's owner closes it
  (log WebSocket: ``4401 unauthorized``).

**Fail closed.** When the database cannot answer, the streams stay open for at
most :data:`MAX_UNVERIFIED_CHECKS` failed re-checks in a row (a few seconds of
a PostgreSQL restart), then every registered stream is closed
(``auth_unavailable``) - an outage never turns into "authenticated forever".

The legacy fabric token (``[controller.auth] legacy_fabric_token``) has no
token row: its streams are not re-checked (the switch is static configuration;
turning it off needs a controller restart, which closes every stream).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Final, Protocol

from meshforge.common.errors import MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.auth.principal import Principal

__all__ = [
    "MAX_UNVERIFIED_CHECKS",
    "LiveStreams",
    "StreamAuth",
    "StreamEndReason",
    "TokenChecker",
]

log = get_logger(__name__)

MAX_UNVERIFIED_CHECKS: Final = 5
"""Consecutive re-checks the database may fail before every stream is closed."""


class StreamEndReason:
    """Why :class:`LiveStreams` ended a stream (``StreamAuth.reason``)."""

    UNAUTHORIZED: Final = "unauthorized"
    """The token was revoked or expired, or its user was disabled."""
    AUTH_UNAVAILABLE: Final = "auth_unavailable"
    """The user database could not confirm the token for too long."""


class TokenChecker(Protocol):
    """What the re-check needs from the user store."""

    async def live_token_ids(self, token_ids: Collection[str]) -> set[str]: ...


@dataclass(eq=False, slots=True)
class StreamAuth:
    """One registered stream (identity, not value, equality)."""

    token_id: str | None
    user_id: str | None
    revoked: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None

    def end(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.revoked.set()


@dataclass(slots=True)
class LiveStreamStats:
    checks: int = 0
    check_failures: int = 0
    streams_cut: int = 0


class LiveStreams:
    """The registry + re-check loop (module docstring). One event loop."""

    def __init__(self, checker: TokenChecker, *, interval_s: float) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._checker = checker
        self.interval_s = interval_s
        self._streams: set[StreamAuth] = set()
        self._wake = asyncio.Event()
        self._failures = 0
        self.stats = LiveStreamStats()

    def __len__(self) -> int:
        return len(self._streams)

    def open(self, principal: Principal) -> StreamAuth:
        """Register a stream authenticated as ``principal``."""
        handle = StreamAuth(token_id=principal.token_id, user_id=principal.user_id)
        self._streams.add(handle)
        return handle

    def close(self, handle: StreamAuth) -> None:
        """The stream ended (idempotent)."""
        self._streams.discard(handle)

    def recheck_now(self) -> None:
        """Re-check before the next tick (a token was just revoked here)."""
        self._wake.set()

    async def check_once(self) -> int:
        """Re-check every registered token; returns the streams ended."""
        checked = [h for h in self._streams if h.token_id is not None]
        if not checked:
            self._failures = 0
            return 0
        self.stats.checks += 1
        wanted = {h.token_id for h in checked if h.token_id is not None}
        try:
            live = await self._checker.live_token_ids(wanted)
        except Exception as exc:  # noqa: BLE001 - a failed check fails closed, never crashes
            self._failures += 1
            self.stats.check_failures += 1
            log.warning(
                "controller.auth.stream_recheck_failed",
                failure_type=exc.failure_type.value
                if isinstance(exc, MeshForgeError)
                else "internal_error",
                cause=type(exc).__name__,
                consecutive_failures=self._failures,
                streams=len(checked),
            )
            if self._failures < MAX_UNVERIFIED_CHECKS:
                return 0
            return self._end(checked, StreamEndReason.AUTH_UNAVAILABLE)
        self._failures = 0
        gone = [h for h in checked if h.token_id not in live]
        return self._end(gone, StreamEndReason.UNAUTHORIZED)

    def _end(self, handles: list[StreamAuth], reason: str) -> int:
        ended = 0
        for handle in handles:
            if handle.revoked.is_set() or handle not in self._streams:
                continue
            handle.end(reason)
            ended += 1
            log.info(
                "controller.auth.stream_closed",
                reason=reason,
                user_id=handle.user_id,
                token_id=handle.token_id,
            )
        self.stats.streams_cut += ended
        return ended

    async def run(self) -> None:
        """Re-check every ``interval_s`` (or at once after :meth:`recheck_now`)."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.interval_s)
            self._wake.clear()
            await self.check_once()
