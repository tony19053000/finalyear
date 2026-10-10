"""V0.7 G3 tester: the agent's revocation set under duplicated, reordered, lost
and hostile updates (D138: at-least-once delivery, union semantics)."""

from __future__ import annotations

import pytest

from meshforge.agent.session.revocations import MAX_REVOKED_SERIALS, RevokedSerials

pytestmark = pytest.mark.unit


def test_duplicate_update_is_idempotent() -> None:
    s = RevokedSerials()
    assert s.add(["ab12", "cd34"], source="update") == 2
    assert s.add(["ab12", "cd34"], source="update") == 0
    assert s() == frozenset({"ab12", "cd34"})


def test_a_late_old_update_never_shrinks_the_set() -> None:
    s = RevokedSerials()
    s.add(["a1", "b2", "c3"], source="update")  # newest
    s.add(["a1"], source="update")  # an old, re-ordered, smaller one
    s.add([], source="hello_ack")
    assert s() == frozenset({"a1", "b2", "c3"})


def test_a_missed_update_is_made_up_by_the_next_full_set() -> None:
    s = RevokedSerials()
    s.add(["a1"], source="update")
    # update #2 ({a1,b2}) was lost; the HelloAck of the reconnect carries all
    s.add(["a1", "b2", "c3"], source="hello_ack")
    assert {"b2", "c3"} <= s()


@pytest.mark.parametrize(
    "bad",
    ["", "AB12", "0x12", "12 ", " 12", "12\n", "g1", "1" * 41, "١٢", "a-b", "../x"],
)
def test_hostile_serials_are_dropped_and_counted(bad: str) -> None:
    s = RevokedSerials()
    s.add([bad, "ab"], source="update")
    assert s() == frozenset({"ab"})
    assert s.malformed == 1


def test_the_set_is_bounded() -> None:
    s = RevokedSerials()
    s.add([format(i, "x") for i in range(MAX_REVOKED_SERIALS + 50)], source="update")
    assert len(s) == MAX_REVOKED_SERIALS
    assert s.overflowed == 50


def test_reader_returns_an_immutable_snapshot() -> None:
    s = RevokedSerials()
    s.add(["aa"], source="update")
    snap = s()
    s.add(["bb"], source="update")
    assert snap == frozenset({"aa"})  # a reader mid-iteration is never mutated
