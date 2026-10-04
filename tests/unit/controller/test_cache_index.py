"""V0.5 G4 D101: cache report validation and sequencing (pure, no database)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.cache_index import (
    MAX_ITEMS,
    CacheIndex,
    Decision,
    NodeCacheState,
    Phase,
    decide,
    validate_report,
)
from meshforge.controller.session.writer import WriteKind

pytestmark = pytest.mark.unit

BLOB = agent_pb2.CacheItem.KIND_BLOB
TREE = agent_pb2.CacheItem.KIND_TREE
ENV = agent_pb2.CacheItem.KIND_ENV
DIGEST = "sha256:" + "a" * 64


def _item(kind: int = BLOB, key: str = DIGEST, size: int = 1) -> agent_pb2.CacheItem:
    return agent_pb2.CacheItem(kind=kind, key=key, size=size)


def _report(
    seq: int,
    *,
    snapshot: bool = False,
    last: bool = False,
    added: list[agent_pb2.CacheItem] | None = None,
    removed: list[agent_pb2.CacheItem] | None = None,
) -> agent_pb2.CacheReport:
    return agent_pb2.CacheReport(
        seq=seq,
        snapshot=snapshot,
        snapshot_last=last,
        added=added or [],
        removed=removed or [],
    )


# --------------------------------------------------------------- validation


def test_valid_report_rows() -> None:
    checked = validate_report(
        _report(
            3,
            added=[_item(), _item(TREE), _item(ENV, "b" * 64, 9)],
            removed=[_item(agent_pb2.CacheItem.KIND_IMAGE)],
        )
    )
    assert not isinstance(checked, str)
    added, removed = checked
    assert [(r.kind, r.key, r.size) for r in added] == [
        ("blob", DIGEST, 1),
        ("tree", DIGEST, 1),
        ("env", "b" * 64, 9),
    ]
    assert [r.kind for r in removed] == ["image"]


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (_report(0, added=[_item()]), "no_seq"),
        (_report(1, last=True), "last_without_snapshot"),
        (_report(1, snapshot=True, removed=[_item()]), "removed_in_snapshot"),
        (_report(1, added=[_item(kind=0)]), "unknown_kind"),
        (_report(1, added=[_item(kind=99)]), "unknown_kind"),
        (_report(1, added=[_item(key="sha256:" + "A" * 64)]), "bad_key"),
        (_report(1, added=[_item(key="a" * 64)]), "bad_key"),
        (_report(1, added=[_item(ENV, DIGEST)]), "bad_key"),
        (_report(1, added=[_item(key=DIGEST + "0")]), "bad_key"),
    ],
)
def test_invalid_reports(report: agent_pb2.CacheReport, reason: str) -> None:
    assert validate_report(report) == reason


def test_item_bound_max_and_max_plus_one() -> None:
    at_max = _report(1, added=[_item()] * (MAX_ITEMS - 1), removed=[_item()])
    assert not isinstance(validate_report(at_max), str)
    over = _report(1, added=[_item()] * MAX_ITEMS, removed=[_item()])
    assert validate_report(over) == "too_many_items"


# --------------------------------------------------------------- sequencing


def _run(state: NodeCacheState, *steps: tuple[int, bool, bool]) -> list[Decision]:
    return [decide(state, seq=s, snapshot=snap, last=last) for s, snap, last in steps]


def test_snapshot_pages_then_deltas() -> None:
    state = NodeCacheState("s1")
    assert _run(
        state,
        (10, True, False),
        (11, True, False),
        (12, True, True),
        (13, False, False),
        (14, False, False),
    ) == [
        Decision.REPLACE,
        Decision.ADD_PAGE,
        Decision.ADD_PAGE,
        Decision.DELTA,
        Decision.DELTA,
    ]
    assert state.phase is Phase.LIVE and state.last_seq == 14


def test_delta_before_the_snapshot_is_dropped_and_inside_it_is_a_gap() -> None:
    state = NodeCacheState("s1")
    assert _run(state, (5, False, False)) == [Decision.EARLY]  # an old replay
    assert _run(state, (6, True, False), (7, False, False)) == [
        Decision.REPLACE,  # right after a delta: a clean start
        Decision.GAP,  # a delta inside a snapshot: never interleaved
    ]
    assert state.phase is Phase.STALE


def test_duplicates_are_ignored() -> None:
    state = NodeCacheState("s1")
    _run(state, (1, True, True), (2, False, False))
    assert _run(state, (2, False, False), (1, True, True), (1, False, False)) == [
        Decision.DUPLICATE,
        Decision.DUPLICATE,
        Decision.DUPLICATE,
    ]
    assert state.phase is Phase.LIVE and state.last_seq == 2


def test_replay_after_reconnect_then_new_snapshot() -> None:
    """A new session first sees the old session's unacked suffix (contiguous
    seqs, possibly starting mid-snapshot), then its own snapshot right after:
    the new snapshot replaces whatever the replay built."""
    state = NodeCacheState("s2")
    assert _run(
        state,
        (5, True, False),  # replay starts mid-way through an old snapshot
        (6, True, True),
        (7, False, False),  # an old delta
        (8, True, False),  # the new session's own snapshot, after a boundary
        (9, True, True),
        (10, False, False),
    ) == [
        Decision.REPLACE,
        Decision.ADD_PAGE,
        Decision.DELTA,
        Decision.REPLACE,
        Decision.ADD_PAGE,
        Decision.DELTA,
    ]
    assert state.phase is Phase.LIVE


def test_gap_makes_the_node_stale_until_a_clean_snapshot() -> None:
    state = NodeCacheState("s1")
    _run(state, (1, True, True))
    assert _run(state, (3, False, False), (4, False, False)) == [
        Decision.GAP,
        Decision.STALE,
    ]
    assert state.phase is Phase.STALE
    assert _run(state, (1, True, True), (5, True, False), (6, True, True)) == [
        Decision.DUPLICATE,
        Decision.REPLACE,  # right after the delta 4: a clean start
        Decision.ADD_PAGE,
    ]
    assert state.phase is Phase.LIVE


def test_a_lost_page_inside_a_snapshot_never_goes_live() -> None:
    state = NodeCacheState("s1")
    assert _run(
        state, (1, True, False), (3, True, False), (4, True, True), (5, False, False)
    ) == [Decision.REPLACE, Decision.GAP, Decision.GAP, Decision.STALE]
    assert state.phase is Phase.STALE
    # The next periodic snapshot (after a boundary) heals it.
    assert _run(state, (6, True, True)) == [Decision.REPLACE]
    assert state.phase is Phase.LIVE


def test_periodic_resnapshot_replaces() -> None:
    state = NodeCacheState("s1")
    _run(state, (1, True, True), (2, False, False))
    assert _run(state, (3, True, False), (4, True, True)) == [
        Decision.REPLACE,
        Decision.ADD_PAGE,
    ]


# ----------------------------------------------------- handler (fake writer)


class _Writer:
    def __init__(self) -> None:
        self.ops: list[tuple[str, WriteKind, Any]] = []

    def enqueue(self, op: str, kind: WriteKind, factory: Any) -> bool:
        self.ops.append((op, kind, factory))
        return True


def _live(session_id: str) -> Any:
    return SimpleNamespace(node_id="node_x", session_id=session_id, writer=_Writer())


def test_handler_fences_state_on_session() -> None:
    index = CacheIndex(store=None)  # type: ignore[arg-type]
    old = _live("s1")
    index.on_report(old, _report(1, snapshot=True, last=True, added=[_item()]))
    index.on_report(old, _report(2, added=[_item(TREE)]))
    assert len(old.writer.ops) == 2
    assert all(kind is WriteKind.CACHE for _, kind, _ in old.writer.ops)
    assert old.writer.ops[0][2].keywords["replace"] is True
    assert old.writer.ops[1][2].keywords["replace"] is False
    # A new session of the node starts over: its delta waits for its snapshot.
    new = _live("s2")
    index.on_report(new, _report(3, added=[_item()]))
    assert new.writer.ops == []
    assert index.stats.dropped == {"early": 1}
    state = index.state("node_x")
    assert state is not None and state.session_id == "s2"


def test_handler_invalid_report_drops_and_goes_stale() -> None:
    index = CacheIndex(store=None)  # type: ignore[arg-type]
    live = _live("s1")
    index.on_report(live, _report(1, snapshot=True, last=True))
    index.on_report(live, _report(2, added=[_item(kind=0)]))
    state = index.state("node_x")
    assert state is not None and state.phase is Phase.STALE
    assert index.stats.dropped == {"unknown_kind": 1}
    index.on_report(live, _report(3, added=[_item()]))
    assert index.stats.dropped == {"unknown_kind": 1, "stale": 1}
    assert len(live.writer.ops) == 1
