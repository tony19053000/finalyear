"""G4 tester round 3: CacheReport.page abuse and old-agent compatibility."""

from __future__ import annotations

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.cache_index import (
    Decision,
    NodeCacheState,
    Phase,
    decide,
    validate_report,
)

pytestmark = pytest.mark.unit


def _rep(seq: int, *, snap: bool, last: bool = False, page: int | None = None):
    r = agent_pb2.CacheReport(seq=seq, snapshot=snap, snapshot_last=last)
    if page is not None:
        r.page = page
    return r


def test_huge_page_index_first_report_is_gap_and_stale() -> None:
    st = NodeCacheState("s")
    assert decide(st, seq=1, snapshot=True, last=True, page=2**32 - 1) is Decision.GAP
    assert st.phase is Phase.STALE


def test_huge_page_index_mid_snapshot_never_goes_live_wrongly() -> None:
    st = NodeCacheState("s")
    assert decide(st, seq=1, snapshot=True, last=False, page=0) is Decision.REPLACE
    # contiguous continuation with a lying page index is still just a continuation
    assert (
        decide(st, seq=2, snapshot=True, last=True, page=2**32 - 1) is Decision.ADD_PAGE
    )
    assert st.phase is Phase.LIVE


def test_page_on_a_delta_is_refused() -> None:
    assert validate_report(_rep(2, snap=False, page=3)) == "page_without_snapshot"


def test_page_zero_repeated_with_new_seq_restarts_not_corrupts() -> None:
    st = NodeCacheState("s")
    assert decide(st, seq=1, snapshot=True, last=False, page=0) is Decision.REPLACE
    assert decide(st, seq=2, snapshot=True, last=False, page=0) is Decision.REPLACE
    assert st.phase is Phase.COLLECTING
    assert decide(st, seq=2, snapshot=True, last=False, page=0) is Decision.DUPLICATE


def test_page_zero_is_the_only_start_after_a_boundary() -> None:
    st = NodeCacheState("s")
    decide(st, seq=1, snapshot=True, last=True, page=0)
    assert decide(st, seq=2, snapshot=True, last=False, page=1) is Decision.GAP
    assert st.phase is Phase.STALE


def test_tail_page_as_first_report_is_stale() -> None:
    st = NodeCacheState("s")
    assert decide(st, seq=9, snapshot=True, last=True, page=4) is Decision.GAP
    assert st.phase is Phase.STALE


def test_old_agent_single_page_snapshot_goes_live() -> None:
    """Old agent: no page field (reads as 0). One-page snapshot is fine."""
    r = _rep(1, snap=True, last=True)
    assert r.page == 0
    st = NodeCacheState("s")
    assert decide(st, seq=1, snapshot=True, last=True, page=r.page) is Decision.REPLACE
    assert st.phase is Phase.LIVE


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-R3-1: old agent's multi-page snapshot (page always 0) is REPLACE on "
    "every page: node goes LIVE holding only the last page's rows",
)
def test_old_agent_multipage_snapshot_must_not_lose_rows() -> None:
    st = NodeCacheState("s")
    decisions = [
        decide(st, seq=1, snapshot=True, last=False, page=0),
        decide(st, seq=2, snapshot=True, last=False, page=0),
        decide(st, seq=3, snapshot=True, last=True, page=0),
    ]
    assert decisions.count(Decision.REPLACE) == 1 or st.phase is not Phase.LIVE


# ------------------------------------------- through the handler (mutation guard)


def test_handler_session_starting_with_a_tail_page_writes_nothing_and_goes_stale() -> (
    None
):
    from types import SimpleNamespace

    from meshforge.controller.cache_index import CacheIndex

    class _W:
        def __init__(self) -> None:
            self.ops: list[object] = []

        def enqueue(self, op: str, kind: object, factory: object) -> bool:
            self.ops.append(factory)
            return True

    index = CacheIndex(store=None)  # type: ignore[arg-type]
    live = SimpleNamespace(node_id="n", session_id="s", writer=_W())
    item = agent_pb2.CacheItem(
        kind=agent_pb2.CacheItem.KIND_BLOB, key="sha256:" + "a" * 64, size=1
    )
    rep = _rep(7, snap=True, last=True, page=3)
    rep.added.append(item)
    index.on_report(live, rep)
    assert live.writer.ops == []
    st = index.state("n")
    assert st is not None and st.phase is Phase.STALE


def test_handler_page_zero_replaces_and_following_pages_add() -> None:
    from types import SimpleNamespace

    from meshforge.controller.cache_index import CacheIndex

    class _W:
        def __init__(self) -> None:
            self.ops: list[object] = []

        def enqueue(self, op: str, kind: object, factory: object) -> bool:
            self.ops.append(factory)
            return True

    index = CacheIndex(store=None)  # type: ignore[arg-type]
    live = SimpleNamespace(node_id="n", session_id="s", writer=_W())
    index.on_report(live, _rep(1, snap=True, last=False, page=0))
    index.on_report(live, _rep(2, snap=True, last=True, page=1))
    reps = [op.keywords["replace"] for op in live.writer.ops]  # type: ignore[attr-defined]
    assert reps == [True, False]
