"""V1.0 G11 (D454): the two GC-index reads the controller's sweep uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.common.gc_index import GcIndex

pytestmark = pytest.mark.unit


def test_items_lists_every_row_of_a_kind_with_size_and_last_use(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    gc = GcIndex(tmp_path / "gc", wall=lambda: now[0])
    gc.open()
    try:
        gc.upsert("blob", "b" * 64, 10)
        now[0] = 2000.0
        gc.upsert("blob", "a" * 64, 20)
        gc.upsert("env", "venv/x", 5)
        assert gc.items("blob") == [("a" * 64, 20, 2000.0), ("b" * 64, 10, 1000.0)]
        assert gc.items("env") == [("venv/x", 5, 2000.0)]
        gc.remove("blob", "a" * 64)
        assert [k for k, _s, _u in gc.items("blob")] == ["b" * 64]
    finally:
        gc.close()


def test_members_of_reads_recorded_trees_and_names_the_unknown_ones(
    tmp_path: Path,
) -> None:
    gc = GcIndex(tmp_path / "gc")
    gc.open()
    try:
        gc.record_tree("t1", ["m1", "m2"])
        gc.record_tree("t2", ["m2", "m3"])
        gc.record_tree("t3", [])  # known, no members
        members, known = gc.members_of(["t1", "t2", "t3", "unknown", "t1"])
        assert members == {"m1", "m2", "m3"}
        assert known == {"t1", "t2", "t3"}
        assert gc.members_of([]) == (set(), set())
    finally:
        gc.close()
