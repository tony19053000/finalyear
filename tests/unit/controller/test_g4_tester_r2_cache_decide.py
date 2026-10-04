"""V0.5 G4 tester round 2: the report sequencing state machine (fix round 1, #5).

Invariant (D101): whenever the node is ``live``, its rows equal the agent's state
at SOME point of the true stream where a message completed (a snapshot's last
page or a delta) - never a mixture, never a partial snapshot - however the
stream is lost / duplicated / reordered. Checked on 30 000 random perturbed
streams against an independent model of the controller's row handling.
"""

from __future__ import annotations

import random

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.cache_index import Decision, NodeCacheState, Phase, decide

pytestmark = pytest.mark.unit


def _stream(
    rnd: random.Random,
) -> tuple[list[tuple[int, bool, bool, set[int], set[int]]], list[frozenset[int]]]:
    """True stream (seq, snapshot, last, added, removed) + the agent states that
    are consistent (after each completed message)."""
    state: set[int] = set()
    msgs: list[tuple[int, bool, bool, set[int], set[int]]] = []
    boundary_states: list[frozenset[int]] = []
    seq = rnd.randint(1, 5)
    for _ in range(rnd.randint(2, 7)):
        if rnd.random() < 0.4 or not msgs:
            for _k in range(rnd.randint(0, 4)):
                state ^= {rnd.randint(0, 20)}
            items = sorted(state)
            pages = [items[i : i + 2] for i in range(0, len(items), 2)] or [[]]
            for n, page in enumerate(pages):
                msgs.append((seq, True, n == len(pages) - 1, set(page), set()))
                seq += 1
            boundary_states.append(frozenset(state))
        else:
            add = {rnd.randint(0, 20) for _ in range(rnd.randint(0, 3))}
            rem = {rnd.randint(0, 20) for _ in range(rnd.randint(0, 3))} - add
            state = (state | add) - rem
            msgs.append((seq, False, False, add, rem))
            seq += 1
            boundary_states.append(frozenset(state))
    return msgs, boundary_states


def _perturb(rnd: random.Random, msgs: list, *, keep_head: bool = False) -> list:
    """Loss, adjacent swaps, duplicates. ``keep_head``: the stream's first message
    is delivered first (a session that begins at the head of a stream)."""
    out = [
        m for i, m in enumerate(msgs) if (keep_head and i == 0) or rnd.random() > 0.15
    ]
    for i in range(1 if keep_head else 0, len(out) - 1):  # adjacent swaps
        if rnd.random() < 0.1:
            out[i], out[i + 1] = out[i + 1], out[i]
    dup = []
    for m in out:
        dup.append(m)
        if rnd.random() < 0.1:
            dup.append(m)  # duplicate
    return dup


def _run(received: list) -> tuple[NodeCacheState, set[int]]:
    st = NodeCacheState("s1")
    rows: set[int] = set()
    for seq, snap, last, add, rem in received:
        d = decide(st, seq=seq, snapshot=snap, last=last)
        if d is Decision.REPLACE:
            rows = set(add)
        elif d is Decision.ADD_PAGE:
            rows |= add
        elif d is Decision.DELTA:
            rows = (rows | add) - rem
    return st, rows


def test_live_rows_are_a_consistent_state_when_the_session_starts_at_a_stream_head() -> (
    None
):
    _fuzz(keep_head=True)


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-R2-1: a session whose FIRST report is the tail page of a "
    "snapshot (older pages acked on the previous session, the replay starts mid-"
    "snapshot) goes LIVE with only that page until the next snapshot",
)
def test_live_rows_are_a_consistent_state_even_when_the_head_is_lost() -> None:
    _fuzz(keep_head=False)


def _fuzz(*, keep_head: bool) -> None:
    rnd = random.Random(20261004)  # noqa: S311
    live = 0
    for _ in range(30_000):
        msgs, ok_states = _stream(rnd)
        received = _perturb(rnd, msgs, keep_head=keep_head)
        st, rows = _run(received)
        if st.phase is Phase.LIVE:
            live += 1
            assert frozenset(rows) in set(ok_states) | {frozenset()}, (
                msgs,
                received,
                rows,
            )
    assert live > 3000, "the generator must reach LIVE often enough to mean something"


def _page(st: NodeCacheState, seq: int, last: bool = False) -> Decision:
    return decide(st, seq=seq, snapshot=True, last=last)


def test_duplicate_first_page_is_ignored_and_does_not_restart_the_snapshot() -> None:
    st = NodeCacheState("s")
    assert _page(st, 1) is Decision.REPLACE
    assert _page(st, 1) is Decision.DUPLICATE
    assert _page(st, 2) is Decision.ADD_PAGE
    assert _page(st, 3, last=True) is Decision.ADD_PAGE
    assert st.phase is Phase.LIVE


def test_out_of_order_last_page_makes_the_node_stale_not_live() -> None:
    st = NodeCacheState("s")
    assert _page(st, 1) is Decision.REPLACE
    assert _page(st, 3, last=True) is Decision.GAP  # 2 is late
    assert st.phase is Phase.STALE
    assert _page(st, 2) is Decision.DUPLICATE  # too late: ignored
    assert st.phase is Phase.STALE
    # ... and the next clean snapshot heals.
    assert _page(st, 4, last=True) is Decision.REPLACE
    assert st.phase is Phase.LIVE


def test_snapshot_interleaved_with_a_delta_is_stale() -> None:
    st = NodeCacheState("s")
    assert _page(st, 1) is Decision.REPLACE
    assert decide(st, seq=2, snapshot=False, last=False) is Decision.GAP
    assert st.phase is Phase.STALE


def test_reconnect_mid_snapshot_a_new_session_starts_clean() -> None:
    """The index keeps one NodeCacheState per session id: pages 1-2 of 3 on the
    old session never mix with the new session's snapshot."""
    old = NodeCacheState("old")
    assert _page(old, 1) is Decision.REPLACE
    assert _page(old, 2) is Decision.ADD_PAGE
    new = NodeCacheState("new")  # the next stream; page 3 of the old one is gone
    assert _page(new, 4) is Decision.REPLACE
    assert _page(new, 5, last=True) is Decision.ADD_PAGE
    assert new.phase is Phase.LIVE


def test_replay_then_snapshot_contiguous_is_accepted_and_a_hole_is_conservative() -> (
    None
):
    """A replay [k..n] then the new snapshot n+1 is accepted. With a seq hole the
    node goes stale (a page without a first-page flag cannot be told from the
    tail of a snapshot whose head was lost) - conservative, healed by the next
    snapshot; pinned so a change is a conscious one."""
    st = NodeCacheState("s")
    assert decide(st, seq=7, snapshot=False, last=False) is Decision.EARLY
    assert decide(st, seq=8, snapshot=False, last=False) is Decision.EARLY
    assert _page(st, 9, last=True) is Decision.REPLACE
    assert st.phase is Phase.LIVE
    hole = NodeCacheState("s2")
    assert decide(hole, seq=7, snapshot=False, last=False) is Decision.EARLY
    assert _page(hole, 9, last=True) is Decision.GAP
    assert hole.phase is Phase.STALE
