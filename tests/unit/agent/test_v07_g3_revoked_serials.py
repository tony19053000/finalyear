"""V0.7 G3 deliverable 6 (D138), agent side: the revocation set the agent
keeps from HelloAck / RevocationUpdate is a bounded union of well-formed
serials - a late, older update never shrinks it."""

from __future__ import annotations

import pytest

from meshforge.agent.session import revocations
from meshforge.agent.session.revocations import RevokedSerials

pytestmark = pytest.mark.unit


def test_union_only_and_malformed_serials_dropped() -> None:
    serials = RevokedSerials(node_id="node_x")
    assert serials() == frozenset()
    assert serials.add(["ab", "0f1", "AB", "0x12", "", "g1", "a" * 41], source="t") == 2
    assert serials() == {"ab", "0f1"}
    assert serials.malformed == 5
    # An older update (a subset) arriving late changes nothing.
    assert serials.add(["ab"], source="t") == 0
    assert serials() == {"ab", "0f1"}
    assert serials.add(["c" * 40], source="t") == 1
    assert len(serials) == 3


def test_the_set_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(revocations, "MAX_REVOKED_SERIALS", 4)
    serials = RevokedSerials()
    assert serials.add(["1", "2", "3"], source="t") == 3
    assert serials.add(["4", "5", "6"], source="t") == 1
    assert len(serials) == 4 and serials.overflowed == 2
    assert serials.add(["7"], source="t") == 0
