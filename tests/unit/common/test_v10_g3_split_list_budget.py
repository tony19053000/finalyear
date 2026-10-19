"""V0.10 G3 - the D278 carry-overs in item resolution.

* The item-list blob is capped **while** items are resolved
  (``SplitLimits.max_list_bytes``): the running count never undercounts the real
  blob (a cap one byte below its size refuses, for every ``over`` kind), it is
  close to it, and it stops a huge ``list_file`` before the JSON is built.
* The ``lines`` file cap is 256 MiB (it is scanned under the GC guard).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Any

import pytest

from meshforge.common.manifest import ManifestError, SplitSpec
from meshforge.common.split_items import ResolvedItems, SplitLimits, resolve
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = pytest.mark.unit


def _tree(root: Path, files: dict[str, bytes]) -> BuiltTree:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return build_tree(root)


def _resolve(built: BuiltTree, over: Any, limits: SplitLimits) -> ResolvedItems:
    def _open(sha: str) -> IO[bytes]:
        return built.sources[sha].open("rb")

    return resolve(
        SplitSpec.model_validate({"over": over}), built.tree, {}, _open, limits
    )


def _cases(tmp_path: Path) -> list[tuple[BuiltTree, Any]]:
    weird = ["é", 'quo"te', "back\\slash", "tab\there", "ctrl\x01x", "日本語"]
    files = {
        f"data/{name}-{i}.bin": os.urandom(3) for i in range(40) for name in ("a", "ü")
    }
    files["data/deep/x y.bin"] = b"1"
    files["items.txt"] = "\n".join([*weird, *(f"it-{i}" for i in range(300))]).encode()
    files["t.csv"] = b"h,v\n" + b"".join(
        f"{i},{'z' * (i % 13)}\n".encode() for i in range(500)
    )
    built = _tree(tmp_path / "p", files)
    return [
        (built, {"glob": "data/**"}),
        (built, {"list": weird}),
        (built, {"list_file": "items.txt"}),
        (built, {"range": {"start": -50_000, "stop": 50_000, "step": 7}}),
        (built, {"lines": "t.csv", "header": True}),
        (built, {"lines": "t.csv"}),
    ]


def test_the_running_count_never_undercounts_the_blob(tmp_path: Path) -> None:
    for built, over in _cases(tmp_path):
        size = len(_resolve(built, over, SplitLimits()).blob())
        with pytest.raises(ManifestError) as info:
            _resolve(built, over, SplitLimits(max_list_bytes=size - 1))
        path, text = info.value.problems[0]
        assert path.startswith("split.over") and "item list would be larger" in text
        # ... and is close to it: a little headroom is enough
        assert _resolve(built, over, SplitLimits(max_list_bytes=size + 300)).count > 0


def test_a_huge_list_file_stops_early(tmp_path: Path) -> None:
    """100 000 items of 1 KiB: refused at the cap, long before the end."""
    line = "x" * 1000
    data = "\n".join(f"{i:06d}{line}" for i in range(100_000)).encode()
    built = _tree(tmp_path / "p", {"big.txt": data})
    limits = SplitLimits(max_list_bytes=1024 * 1024, max_list_file_bytes=200 * 1024**2)
    with pytest.raises(ManifestError) as info:
        _resolve(built, {"list_file": "big.txt"}, limits)
    assert "1048576 bytes" in info.value.problems[0][1]


def test_lines_files_are_capped_at_256_mib_by_default() -> None:
    assert SplitLimits().max_lines_file_bytes == 256 * 1024 * 1024
    with pytest.raises(ValueError, match="positive"):
        SplitLimits(max_list_bytes=0)
