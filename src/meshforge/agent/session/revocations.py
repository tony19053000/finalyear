"""The fabric's revocation set as this agent knows it (V0.7 G3, D138).

The controller sends the set in every ``HelloAck`` (``revoked_serials``) and
as a ``RevocationUpdate`` command whenever it changes (re-sent on a missing
Ack, D113). The agent keeps the **union** of everything it got - serials are
only ever added (there is no un-revoke), so an old update delivered late can
never shrink the set. The peer blob server reads it at every connection and
every request and refuses a client certificate whose serial is listed.

Bounded: a serial is lower-case hex of at most 40 characters (anything else is
dropped and counted); at most :data:`MAX_REVOKED_SERIALS` are kept (more is
logged as an error - far beyond any real fabric). Memory only: a restarted
agent gets the whole set again in the first ``HelloAck``, before its peer
server can verify any token (the token key arrives in the same ``HelloAck``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from meshforge.common.logging import get_logger

__all__ = ["MAX_REVOKED_SERIALS", "RevokedSerials"]

log = get_logger(__name__)

MAX_REVOKED_SERIALS: Final = 100_000
_SERIAL: Final = re.compile(r"[0-9a-f]{1,40}")


class RevokedSerials:
    """See module docstring. Event-loop owned; ``__call__`` is the reader."""

    def __init__(self, *, node_id: str = "") -> None:
        self._node_id = node_id
        self._serials: frozenset[str] = frozenset()
        self.updates = 0
        self.malformed = 0
        self.overflowed = 0

    def __call__(self) -> frozenset[str]:
        return self._serials

    def __len__(self) -> int:
        return len(self._serials)

    def add(self, serials: Iterable[str], *, source: str) -> int:
        """Add the well-formed ``serials``; how many were new."""
        fresh: set[str] = set()
        for serial in serials:
            if not isinstance(serial, str) or not _SERIAL.fullmatch(serial):
                self.malformed += 1
                continue
            if serial not in self._serials:
                fresh.add(serial)
        room = MAX_REVOKED_SERIALS - len(self._serials)
        if len(fresh) > room:
            self.overflowed += len(fresh) - max(room, 0)
            log.error(
                "agent.revocation.set_full",
                node_id=self._node_id,
                kept=len(self._serials),
                dropped=len(fresh) - max(room, 0),
            )
            fresh = set(sorted(fresh)[: max(room, 0)])
        if fresh:
            self._serials = self._serials | fresh
        self.updates += 1
        log.info(
            "agent.revocation.updated",
            node_id=self._node_id,
            source=source,
            added=len(fresh),
            total=len(self._serials),
        )
        return len(fresh)
