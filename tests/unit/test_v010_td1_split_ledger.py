"""V0.10 TD1 (D288 follow-up): the cluster tests' exactly-once ledger check
(``tests/_support/mf_split_ledger.py``) fails on a leaked ``live`` lease and on
the other broken ledgers it names, and passes on a clean one.

The rows are hand-built (``SimpleNamespace`` with the columns
``check_exactly_once`` selects), so this runs without a database.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from mf_split_ledger import assert_exactly_once

pytestmark = pytest.mark.unit

ITEMS = 10


def _lease(
    idx: int, seq: int | None, state: str | None, *, done_seq: int, digest: str
) -> SimpleNamespace:
    return SimpleNamespace(
        idx=idx,
        item_state="done",
        done_lease_seq=done_seq,
        result_digest=digest,
        seq=seq,
        state=state,
        holder=f"lane-{idx}-{seq}" if seq is not None else None,
    )


def _record(
    chunk: int,
    seq: int,
    outcome: str,
    *,
    first: int,
    items: int,
    digest: str | None,
    won: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_index=chunk,
        lease_seq=seq,
        outcome=outcome,
        first_item=first,
        items=items,
        result_digest=digest,
        won=won,
    )


def _clean() -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
    """Three chunks (4 + 4 + 2 items). Chunk 1's first lease was released (its
    lane was lost) and its second completed; chunk 2 had a speculative copy
    that the winner superseded."""
    leases = [
        _lease(0, 1, "completed", done_seq=1, digest="d0"),
        _lease(1, 1, "released", done_seq=2, digest="d1"),
        _lease(1, 2, "completed", done_seq=2, digest="d1"),
        _lease(2, 1, "completed", done_seq=1, digest="d2"),
        _lease(2, 2, "superseded", done_seq=1, digest="d2"),
    ]
    records = [
        _record(0, 1, "committed", first=0, items=4, digest="d0", won=True),
        _record(1, 1, "released", first=4, items=4, digest=None, won=False),
        _record(1, 2, "committed", first=4, items=4, digest="d1", won=True),
        _record(2, 1, "committed", first=8, items=2, digest="d2", won=True),
        _record(2, 2, "superseded", first=8, items=2, digest=None, won=False),
    ]
    return leases, records


def test_a_clean_ledger_passes() -> None:
    leases, records = _clean()
    assert_exactly_once(leases, records, ITEMS)


def test_a_leftover_live_lease_fails_even_when_every_other_rule_holds() -> None:
    """The D288 gap: chunk 2's speculative copy is still ``live`` after the job
    ended. It even has a record, so the chunk/record/lease-set rules all hold -
    only the live-lease rule catches the leak."""
    leases, records = _clean()
    leases[4].state = "live"
    with pytest.raises(AssertionError, match="still live") as caught:
        assert_exactly_once(leases, records, ITEMS)
    assert "(2, 2, 'lane-2-2')" in str(caught.value)


def test_a_leftover_live_lease_without_a_record_fails_on_the_live_rule() -> None:
    leases, records = _clean()
    leases.append(_lease(0, 2, "live", done_seq=1, digest="d0"))
    with pytest.raises(AssertionError, match="still live"):
        assert_exactly_once(leases, records, ITEMS)


def _drop_a_record(leases: list[Any], records: list[Any]) -> None:
    del records[1]  # chunk 1's released lease has no row


def _second_completed(leases: list[Any], records: list[Any]) -> None:
    leases[1].state = "completed"


def _gap_in_items(leases: list[Any], records: list[Any]) -> None:
    records[2].first_item = 5


def _orphan_record(leases: list[Any], records: list[Any]) -> None:
    records.append(_record(0, 9, "expired", first=0, items=4, digest=None, won=False))


def _item_not_done(leases: list[Any], records: list[Any]) -> None:
    leases[0].item_state = "leased"


def _wrong_digest(leases: list[Any], records: list[Any]) -> None:
    records[0].result_digest = "other"


@pytest.mark.parametrize(
    "breaks",
    [
        _drop_a_record,
        _second_completed,
        _gap_in_items,
        _orphan_record,
        _item_not_done,
        _wrong_digest,
    ],
)
def test_other_broken_ledgers_still_fail(
    breaks: Callable[[list[Any], list[Any]], None],
) -> None:
    leases, records = _clean()
    breaks(leases, records)
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, ITEMS)


def test_the_item_count_must_match() -> None:
    leases, records = _clean()
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, ITEMS + 1)
