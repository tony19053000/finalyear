"""V0.10 G5 fix round 1: the slice-digest memo key (T2), slot-kind-matched
draining lanes (N2) and the busy-refusal settings (B1)."""

from __future__ import annotations

import pytest

from meshforge.common.split_chunks import ItemList, chunk_items_digest
from meshforge.controller.jobs.planning import _without_draining
from meshforge.controller.planner.types import ClusterSnapshot, NodeSnapshot
from meshforge.controller.settings import SplitSettings
from meshforge.controller.split.combine import CombineBusy
from meshforge.controller.split.store import ItemLists

pytestmark = [pytest.mark.unit]

DIGEST = "sha256:" + "c" * 64


# ------------------------------------------------------------------ T2


def _lists(items: ItemList) -> ItemLists:
    lists = ItemLists(None)
    lists._cache[DIGEST] = (items, {})  # as get() leaves it after a read
    return lists


def test_slice_digest_memo_keys_on_first_and_count() -> None:
    """Two jobs sharing one item list with chunk sizes 2 and 3: chunk 0 of
    each has its own digest (the memo once returned the first one cached)."""
    items = ItemList(over="list", items=tuple(f"i{n}" for n in range(6)))
    lists = _lists(items)
    two = lists.slice_digest(DIGEST, items, 0, 2)
    three = lists.slice_digest(DIGEST, items, 0, 3)
    assert two == str(chunk_items_digest(["i0", "i1"]))
    assert three == str(chunk_items_digest(["i0", "i1", "i2"]))
    assert two != three
    # memoised per (first, count): asked again, the same answers
    assert lists.slice_digest(DIGEST, items, 0, 2) == two
    assert lists.slice_digest(DIGEST, items, 0, 3) == three
    assert set(lists._cache[DIGEST][1]) == {(0, 2), (0, 3)}


def test_slice_digest_of_an_uncached_list_is_still_right() -> None:
    items = ItemList(over="range", items=(0, 1, 2, 3))
    lists = ItemLists(None)
    assert lists.slice_digest(DIGEST, items, 1, 2) == str(chunk_items_digest([1, 2]))
    assert lists.slice_digest(DIGEST, items, 1, 3) == str(chunk_items_digest([1, 2, 3]))


# ------------------------------------------------------------------ N2


def _node(**kw: int) -> NodeSnapshot:
    return NodeSnapshot(
        node_id="n1",
        state="online",
        connected=True,
        capabilities=frozenset(),
        cpu_slots=1,
        gpu_slots=1,
        **kw,
    )


def test_draining_cpu_lane_frees_its_cpu_slot_for_the_combine() -> None:
    snap = ClusterSnapshot(nodes=(_node(used_cpu_slots=1, live_attempts=1),))
    out = _without_draining(snap, {"n1": (1, 0)})
    assert out.nodes[0].used_cpu_slots == 0
    assert out.nodes[0].live_attempts == 0


def test_draining_gpu_lane_frees_no_cpu_slot() -> None:
    """A combine needs a CPU slot; a draining GPU lane still runs as far as
    the node is concerned (its live attempts stay)."""
    snap = ClusterSnapshot(
        nodes=(_node(used_cpu_slots=1, used_gpu_slots=1, live_attempts=2),)
    )
    out = _without_draining(snap, {"n1": (0, 1)})
    node = out.nodes[0]
    assert (node.used_cpu_slots, node.used_gpu_slots, node.live_attempts) == (1, 1, 2)
    # both kinds draining: only the CPU one is freed
    out = _without_draining(snap, {"n1": (1, 1)})
    node = out.nodes[0]
    assert (node.used_cpu_slots, node.used_gpu_slots, node.live_attempts) == (0, 1, 1)


# ------------------------------------------------------------------ B1


def test_busy_refusal_settings_defaults_and_bounds() -> None:
    cfg = SplitSettings()
    assert cfg.combine_busy_backoff_s == 1.0
    assert cfg.combine_busy_max == 100
    assert CombineBusy() == CombineBusy(backoff_s=1.0, max_free=100)
    with pytest.raises(ValueError):
        SplitSettings(combine_busy_backoff_s=-1.0)
    with pytest.raises(ValueError):
        SplitSettings(combine_busy_max=-1)
