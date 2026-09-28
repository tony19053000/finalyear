"""V0.4 G1 (D42): :class:`AdmissionClaims`, the D28 claim / deferred-end bookkeeping
split out of ``manager.py``, tested on its own.

Pure: no manager, no registry, no clock reads. Each test states the D28 rule it
pins (``claims.py`` module docstring is the spec).
"""

from __future__ import annotations

import asyncio

import pytest

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.controller.liveness import LeaseTracker, LivenessConfig, NodeState
from meshforge.controller.session.claims import AdmissionClaims
from meshforge.controller.session.live import LiveSession, SeenIds
from meshforge.controller.session.writer import WriteChain

pytestmark = pytest.mark.unit


def session(node_id: str, epoch: int) -> LiveSession:
    session_id = new_session_id()
    return LiveSession(
        node_id=node_id,
        session_id=session_id,
        session_epoch=epoch,
        peer_address="ipv4:127.0.0.1:1",
        tracker=LeaseTracker(LivenessConfig(), lambda: 0.0),
        state=NodeState.ONLINE,
        outbound=asyncio.Queue(),
        seen=SeenIds(4),
        next_ping_at=0.0,
        writer=WriteChain(node_id=node_id, session_id=session_id),
    )


def test_nothing_is_claimed_without_a_pending_hello() -> None:
    claims = AdmissionClaims()
    live = session(new_node_id(), 3)
    assert not claims.owns(live)


def test_a_higher_epoch_claims_the_older_session() -> None:
    claims = AdmissionClaims()
    node = new_node_id()
    live = session(node, 3)
    claims.claim(node, 4)
    assert claims.owns(live)
    assert not claims.owns(session(new_node_id(), 1))  # other nodes are untouched


@pytest.mark.parametrize("epoch", [2, 3])
def test_a_stale_hello_claims_nothing(epoch: int) -> None:
    """Equal is stale too (ADR-0002 §1): a stale Hello can never suppress a close."""
    claims = AdmissionClaims()
    node = new_node_id()
    live = session(node, 3)
    claims.claim(node, epoch)
    assert not claims.owns(live)
    assert claims.release(node, epoch) is None


def test_a_closing_manager_is_never_claimed() -> None:
    claims = AdmissionClaims()
    node = new_node_id()
    live = session(node, 1)
    claims.claim(node, 2)
    assert claims.owns(live)
    assert not claims.owns(live, closing=True)


def test_release_of_the_last_claim_forgets_the_node() -> None:
    claims = AdmissionClaims()
    node = new_node_id()
    claims.claim(node, 5)
    claims.claim(node, 6)
    assert claims.release(node, 5) is None
    assert claims.pending == {node: [6]}
    assert claims.release(node, 6) is None
    assert claims.pending == {}


def test_deferred_end_with_a_winner_is_consumed_without_an_event() -> None:
    """Success path: the admission consumes the deferred end, release returns
    nothing to settle."""
    claims = AdmissionClaims()
    node = new_node_id()
    old = session(node, 1)
    claims.claim(node, 2)
    assert claims.defer(old) is None
    assert claims.deferred_for(node) is old
    assert claims.consume(old) is True
    assert claims.consume(old) is False  # idempotent: already taken
    assert claims.release(node, 2) is None
    assert claims.deferred == {}


def test_deferred_end_without_a_winner_is_handed_back_to_settle() -> None:
    """Failure path: nobody consumed it, so release returns it (the manager then
    applies the event it would have fired)."""
    claims = AdmissionClaims()
    node = new_node_id()
    old = session(node, 1)
    claims.claim(node, 2)
    claims.defer(old)
    assert claims.release(node, 2) is old
    assert claims.deferred == {}
    assert claims.pending == {}


def test_a_deferred_end_still_owned_by_another_pending_hello_is_kept() -> None:
    claims = AdmissionClaims()
    node = new_node_id()
    old = session(node, 1)
    claims.claim(node, 2)
    claims.claim(node, 3)
    claims.defer(old)
    assert claims.release(node, 2) is None  # epoch 3 still owns it
    assert claims.deferred_for(node) is old
    assert claims.release(node, 3) is old


def test_release_while_closing_settles_the_deferred_end() -> None:
    claims = AdmissionClaims()
    node = new_node_id()
    old = session(node, 1)
    claims.claim(node, 2)
    claims.claim(node, 3)
    claims.defer(old)
    assert claims.release(node, 2, closing=True) is old


def test_a_second_deferred_end_for_one_node_hands_back_the_first() -> None:
    """Never expected (a node has one live session) - but if it happens, the
    displaced session is returned so its chain still gets its end."""
    claims = AdmissionClaims()
    node = new_node_id()
    first, second = session(node, 1), session(node, 2)
    claims.claim(node, 3)
    assert claims.defer(first) is None
    assert claims.defer(first) is None  # re-deferring the same one displaces nothing
    assert claims.defer(second) is first
    assert claims.deferred_for(node) is second


def test_drain_returns_and_forgets_every_deferred_end() -> None:
    claims = AdmissionClaims()
    a, b = session(new_node_id(), 1), session(new_node_id(), 1)
    claims.claim(a.node_id, 2)
    claims.claim(b.node_id, 2)
    claims.defer(a)
    claims.defer(b)
    assert set(claims.drain()) == {a, b}
    assert claims.deferred == {}
    assert claims.drain() == []
