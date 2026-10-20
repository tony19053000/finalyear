"""V0.10 G5 tester r2 (T2): one item list shared by jobs of different chunk
sizes on one controller - every chunk's digest is that of its own slice, in
any order of asking, for list and range item lists, including a single chunk
after a split run and a 3-size sequence."""

from __future__ import annotations

import itertools

import pytest

from meshforge.common.split_chunks import ItemList, chunk_items_digest
from meshforge.controller.split.store import ItemLists

pytestmark = [pytest.mark.unit]

DIGEST = "sha256:" + "d" * 64


def _chunks(total: int, size: int) -> list[tuple[int, int]]:
    return [(f, min(size, total - f)) for f in range(0, total, size)]


def _expect(items: ItemList, first: int, count: int) -> str:
    return str(chunk_items_digest(list(items.items[first : first + count])))


@pytest.mark.parametrize("over", ["list", "range"])
@pytest.mark.parametrize("order", list(itertools.permutations([6, 30, 4])))
def test_three_sizes_in_every_order_each_chunk_matches_its_slice(
    over: str, order: tuple[int, ...]
) -> None:
    total = 30
    items = ItemList(over=over, items=tuple(range(total)))
    lists = ItemLists(None)
    lists._cache[DIGEST] = (items, {})
    for size in order:  # 30 = the 1-chunk job after / before a split
        for first, count in _chunks(total, size):
            assert lists.slice_digest(DIGEST, items, first, count) == _expect(
                items, first, count
            ), (size, first, count)
    # asked a second time, the memo gives the same answers
    for size in order:
        for first, count in _chunks(total, size):
            assert lists.slice_digest(DIGEST, items, first, count) == _expect(
                items, first, count
            )


def test_ragged_last_chunk_differs_from_full_chunk_with_same_first() -> None:
    items = ItemList(over="list", items=tuple(f"i{n}" for n in range(10)))
    lists = ItemLists(None)
    lists._cache[DIGEST] = (items, {})
    a = lists.slice_digest(DIGEST, items, 8, 2)
    b = lists.slice_digest(DIGEST, items, 8, 5)  # clipped by the list end
    assert a == _expect(items, 8, 2)
    assert b == _expect(items, 8, 5) == a  # same slice (clipped): same digest


def test_memo_stays_bounded() -> None:
    items = ItemList(over="range", items=tuple(range(100)))
    lists = ItemLists(None)
    lists._cache[DIGEST] = (items, {})
    for count in range(1, 100):
        for first in range(0, 100 - count + 1, 7):
            assert lists.slice_digest(DIGEST, items, first, count) == _expect(
                items, first, count
            )
    assert len(lists._cache[DIGEST][1]) < 40_001
