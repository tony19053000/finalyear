"""V0.10 G9 tester: does ``check_exactly_once`` (the ledger oracle of every Mode 3
cluster test) actually detect violations?  A weak oracle makes the cluster tests
vacuous, so it is fed a valid ledger (must pass) and doctored ones (each must raise).
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace as NS  # noqa: N814
from typing import Any

import mf_split_cluster as msc
import pytest

ITEMS = 10


def _lease(idx: int, seq: int | None, state: str | None, **kw: Any) -> NS:
    base: dict[str, Any] = {
        "idx": idx,
        "item_state": "done",
        "done_lease_seq": kw.pop("done_lease_seq", 1),
        "result_digest": f"sha256:{idx}",
        "seq": seq,
        "state": state,
        "holder": "att",
    }
    base.update(kw)
    return NS(**base)


def _rec(idx: int, seq: int, outcome: str, first: int, n: int, **kw: Any) -> NS:
    base: dict[str, Any] = {
        "chunk_index": idx,
        "lease_seq": seq,
        "lane_attempt_id": "att",
        "node_id": "n",
        "first_item": first,
        "items": n,
        "speculative": False,
        "won": outcome == "committed",
        "outcome": outcome,
        "result_digest": f"sha256:{idx}",
        "leased_at": None,
        "ended_at": None,
    }
    base.update(kw)
    return NS(**base)


def _valid() -> tuple[list[NS], list[NS]]:
    leases = [
        _lease(0, 1, "completed", done_lease_seq=1),
        _lease(1, 1, "released", done_lease_seq=2),
        _lease(1, 2, "completed", done_lease_seq=2),
        _lease(2, 1, "completed", done_lease_seq=1),
    ]
    records = [
        _rec(0, 1, "committed", 0, 4),
        _rec(1, 1, "released", 4, 3),
        _rec(1, 2, "committed", 4, 3),
        _rec(2, 1, "committed", 7, 3),
    ]
    return leases, records


def _run(
    monkeypatch: pytest.MonkeyPatch, leases: list[NS], records: list[NS], items: int
) -> None:
    def fake(_pg: Any, query: str, **_: Any) -> list[NS]:
        return leases if "work_item" in query else records

    monkeypatch.setattr(msc, "sql", fake)
    msc.check_exactly_once(None, "job", items)


def test_valid_ledger_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    leases, records = _valid()
    _run(monkeypatch, leases, records, ITEMS)


def _dup_won(le: list[NS], rc: list[NS]) -> None:
    rc.append(_rec(1, 1, "committed", 4, 3))  # a second committed row, chunk 1


def _dup_won_other_lease(le: list[NS], rc: list[NS]) -> None:
    rc[1] = _rec(1, 1, "committed", 4, 3)  # the released lease also "won"


def _gap(le: list[NS], rc: list[NS]) -> None:
    rc[3] = _rec(2, 1, "committed", 8, 2)  # items 7 missing


def _overlap(le: list[NS], rc: list[NS]) -> None:
    rc[3] = _rec(2, 1, "committed", 6, 4)


def _short_total(le: list[NS], rc: list[NS]) -> None:
    rc[3] = _rec(2, 1, "committed", 7, 2)  # only 9 items covered


def _lease_without_record(le: list[NS], rc: list[NS]) -> None:
    le.append(_lease(2, 2, "failed", done_lease_seq=1))
    # no chunk_record for (2, 2)


def _record_without_lease(le: list[NS], rc: list[NS]) -> None:
    rc.append(_rec(2, 9, "released", 7, 3))


def _two_completed(le: list[NS], rc: list[NS]) -> None:
    le.append(_lease(0, 2, "completed", done_lease_seq=1))
    rc.append(_rec(0, 2, "released", 0, 4))


def _item_not_done(le: list[NS], rc: list[NS]) -> None:
    le[3] = _lease(2, 1, "completed", item_state="leased")


def _no_completed(le: list[NS], rc: list[NS]) -> None:
    le[3] = _lease(2, 1, "live")


def _wrong_winner_seq(le: list[NS], rc: list[NS]) -> None:
    rc[2] = _rec(1, 2, "committed", 4, 3)
    le[2] = _lease(1, 2, "completed", done_lease_seq=1)  # item names another lease


def _digest_mismatch(le: list[NS], rc: list[NS]) -> None:
    rc[0] = _rec(0, 1, "committed", 0, 4, result_digest="sha256:evil")


def _missing_chunk_index(le: list[NS], rc: list[NS]) -> None:
    del le[3]
    le.append(_lease(3, 1, "completed"))  # chunks 0,1,3: a hole at 2
    rc[3] = _rec(3, 1, "committed", 7, 3)


def _chunk_without_committed(le: list[NS], rc: list[NS]) -> None:
    rc[3] = _rec(2, 1, "released", 7, 3)


def _won_but_not_committed(le: list[NS], rc: list[NS]) -> None:
    rc[1] = _rec(1, 1, "released", 4, 3, won=True)  # 5 winners for 3 chunks


def _zero_items_chunk(le: list[NS], rc: list[NS]) -> None:
    rc[0] = _rec(0, 1, "committed", 0, 0)


def _out_of_order(le: list[NS], rc: list[NS]) -> None:
    rc[0] = _rec(0, 1, "committed", 6, 4)
    rc[3] = _rec(2, 1, "committed", 0, 3)


MUTATIONS: dict[str, Callable[[list[NS], list[NS]], None]] = {
    "duplicate-won": _dup_won,
    "released-lease-also-won": _dup_won_other_lease,
    "gap-in-items": _gap,
    "overlap-in-items": _overlap,
    "short-total": _short_total,
    "lease-without-record": _lease_without_record,
    "record-without-lease": _record_without_lease,
    "two-completed-leases": _two_completed,
    "item-not-done": _item_not_done,
    "no-completed-lease": _no_completed,
    "wrong-winner-seq": _wrong_winner_seq,
    "digest-mismatch": _digest_mismatch,
    "hole-in-chunk-indexes": _missing_chunk_index,
    "chunk-without-committed": _chunk_without_committed,
    "won-but-not-committed": _won_but_not_committed,
    "zero-item-chunk": _zero_items_chunk,
    "out-of-order-spans": _out_of_order,
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_doctored_ledger_is_detected(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    leases, records = _valid()
    MUTATIONS[name](leases, records)
    with pytest.raises(AssertionError):
        _run(monkeypatch, leases, records, ITEMS)


def test_wrong_expected_item_count_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leases, records = _valid()
    with pytest.raises(AssertionError):
        _run(monkeypatch, leases, records, ITEMS + 1)


def test_empty_ledger_is_not_vacuously_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AssertionError):
        _run(monkeypatch, [], [], 0)
