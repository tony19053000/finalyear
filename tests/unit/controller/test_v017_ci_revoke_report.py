"""CI fix (2026-10-27): a REST revoke reports ``session_killed`` truthfully when
the periodic sync cut the session first.

The periodic :meth:`Revocations.run` sync can run between the REST revoke's
commit and the request's own sync; it cuts the session (``node_revoked``) and
the request's sync then finds no session. The request still caused the cut, so
it answers ``session_killed=true``. A second revoke of the same node (already
revoked) still reports nothing cut.
"""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.controller.revocation import (
    Revocations,
    RevocationSnapshot,
    RevokeOutcome,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.security]


class Sessions:
    def __init__(self, live: set[str]) -> None:
        self.live = live
        self.cut: list[str] = []

    def has_session(self, node_id: str) -> bool:
        return node_id in self.live

    async def revoke_node(self, node_id: str) -> bool:
        self.cut.append(node_id)
        self.live.discard(node_id)
        return True

    async def cut_revoked_certs(self, serials: Any) -> frozenset[str]:
        return frozenset()

    def broadcast(self, payload_name: str, payload: Any) -> int:
        return 1


class Store:
    """Commits the revocation, then lets the periodic sync run before the
    REST request's own sync (``between`` hook)."""

    def __init__(self) -> None:
        self.snap = RevocationSnapshot(frozenset(), frozenset(), ())
        self.revoked: set[str] = set()
        self.between: Any = None

    async def snapshot(self) -> RevocationSnapshot:
        return self.snap

    async def revoke_node(
        self, node_id: str, *, actor: str, source: str | None = None
    ) -> RevokeOutcome:
        already = node_id in self.revoked
        self.revoked.add(node_id)
        self.snap = RevocationSnapshot(
            frozenset(self.revoked), frozenset({"aa"}), ("aa",)
        )
        if self.between is not None:
            await self.between()
        return RevokeOutcome(node_id, "aa", already, "online")


async def test_a_cut_by_the_periodic_sync_in_between_is_still_reported() -> None:
    store, sessions = Store(), Sessions({"n1"})
    revocations = Revocations(store, sessions)  # type: ignore[arg-type]
    store.between = revocations.sync  # the periodic sync wins the race
    outcome = await revocations.revoke("n1", actor="t")
    assert sessions.cut == ["n1"]  # cut once, by the periodic sync
    assert outcome.session_killed is True
    assert outcome.as_dict()["session_killed"] is True

    store.between = None
    again = await revocations.revoke("n1", actor="t")
    assert again.already is True and again.session_killed is False


async def test_a_periodic_cut_of_an_earlier_revocation_is_not_claimed() -> None:
    """The CLI revoked n1 (another process) and the periodic sync cut it; a
    later REST revoke of n1 is ``already`` and did not cut anything."""
    store, sessions = Store(), Sessions({"n1"})
    revocations = Revocations(store, sessions)  # type: ignore[arg-type]
    store.revoked.add("n1")
    store.snap = RevocationSnapshot(frozenset({"n1"}), frozenset({"aa"}), ("aa",))
    assert await revocations.sync() == frozenset({"n1"})
    outcome = await revocations.revoke("n1", actor="t")
    assert outcome.already is True and outcome.session_killed is False


async def test_no_session_no_kill() -> None:
    store, sessions = Store(), Sessions(set())
    revocations = Revocations(store, sessions)  # type: ignore[arg-type]
    outcome = await revocations.revoke("n1", actor="t")
    assert outcome.session_killed is False and sessions.cut == []


async def test_an_unreported_cut_is_forgotten_once_the_node_is_not_revoked() -> None:
    store, sessions = Store(), Sessions({"n1"})
    revocations = Revocations(store, sessions)  # type: ignore[arg-type]
    store.revoked.add("n1")
    store.snap = RevocationSnapshot(frozenset({"n1"}), frozenset({"aa"}), ("aa",))
    assert await revocations.sync() == frozenset({"n1"})  # CLI revoke, cut
    store.revoked.clear()  # reinstated (re-enrolled), no session now
    store.snap = RevocationSnapshot(frozenset(), frozenset({"aa"}), ("aa",))
    await revocations.sync()
    outcome = await revocations.revoke("n1", actor="t")
    assert outcome.already is False and outcome.session_killed is False
