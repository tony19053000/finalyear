"""V0.10 G1 (0.10.1, D260, D273): resolving ``split.over`` to items.

Real trees built from real folders (``build_tree``), the resolver the
controller and ``compute run --dry-run`` share. Ordering is bytewise and fixed
(checklist 33), every refusal names its YAML path, caps stop early, symlinks
are followed only inside their tree, and the item-list blob is deterministic.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import IO, Any

import pytest

from meshforge.common.hashing import sha256_bytes
from meshforge.common.manifest import ManifestError, SplitSpec
from meshforge.common.split_items import (
    ResolvedItems,
    SplitLimits,
    glob_match,
    glob_parts,
    may_enter,
    resolve,
)
from meshforge.common.tree import BuiltTree, Tree, build_tree


def _tree(
    root: Path, files: dict[str, bytes], links: dict[str, str] | None = None
) -> BuiltTree:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name, target in (links or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    return build_tree(root)


def _opener(*built: BuiltTree) -> Any:
    sources: dict[str, Path] = {}
    for b in built:
        sources.update(b.sources)

    def _open(sha: str) -> IO[bytes]:
        return sources[sha].open("rb")

    return _open


def _spec(over: Any, **extra: Any) -> SplitSpec:
    return SplitSpec.model_validate({"over": over, **extra})


def _resolve(
    built: BuiltTree,
    over: Any,
    *,
    inputs: dict[str, BuiltTree] | None = None,
    limits: SplitLimits | None = None,
    **extra: Any,
) -> ResolvedItems:
    inputs = inputs or {}
    trees: dict[str, Tree] = {m: b.tree for m, b in inputs.items()}
    return resolve(
        _spec(over, **extra),
        built.tree,
        trees,
        _opener(built, *inputs.values()),
        limits,
    )


def _refused(built: BuiltTree, over: Any, **kw: Any) -> tuple[str, str]:
    with pytest.raises(ManifestError) as info:
        _resolve(built, over, **kw)
    assert info.value.failure_type.value == "invalid_input"
    return info.value.problems[0]


# ------------------------------------------------------------------- glob


def test_glob_items_are_files_sorted_bytewise(tmp_path: Path) -> None:
    names = ["b.jpg", "B.jpg", "a.jpg", "é.jpg", "z.jpg", "Z.jpg", "10.jpg", "9.jpg"]
    built = _tree(
        tmp_path / "p", {f"img/{n}": n.encode() for n in names} | {"x.txt": b"x"}
    )
    got = _resolve(built, "img/*.jpg")
    expected = sorted((f"img/{n}" for n in names), key=lambda p: p.encode("utf-8"))
    assert list(got.items) == expected
    # bytewise: upper case before lower case, digits before both, non-ASCII last
    assert got.items[0] == "img/10.jpg" and got.items[-1] == "img/é.jpg"
    assert [f.path for f in got.files] == expected
    assert [f.sha256 for f in got.files] == [
        sha256_bytes(p.removeprefix("img/").encode()).hex for p in expected
    ]
    assert got.item_bytes == sum(len(n.encode()) for n in names)


def test_glob_order_does_not_depend_on_the_folder_walk(tmp_path: Path) -> None:
    files = {f"d{i % 3}/f{i:03d}.bin": bytes([i]) for i in range(60)}
    a = _resolve(_tree(tmp_path / "a", files), "**/*.bin")
    b = _resolve(_tree(tmp_path / "b", dict(reversed(files.items()))), "**/*.bin")
    assert a.items == b.items and a.blob() == b.blob() and a.digest == b.digest


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*.jpg", ["top.jpg"]),
        ("**/*.jpg", ["a/b/deep.jpg", "a/mid.jpg", "top.jpg"]),
        ("a/**", ["a/b/deep.jpg", "a/mid.jpg", "a/note.txt"]),
        ("a/*", ["a/mid.jpg", "a/note.txt"]),
        ("a/**/*.jpg", ["a/b/deep.jpg", "a/mid.jpg"]),
        ("?op.jpg", ["top.jpg"]),
        ("[at]*.jpg", ["top.jpg"]),
        ("a/b", []),  # a folder is never an item
    ],
)
def test_glob_rules_match_outputs(
    tmp_path: Path, pattern: str, expected: list[str]
) -> None:
    built = _tree(
        tmp_path / "p",
        {"top.jpg": b"1", "a/mid.jpg": b"2", "a/b/deep.jpg": b"3", "a/note.txt": b"4"},
    )
    if expected:
        assert list(_resolve(built, pattern).items) == expected
    else:
        assert _refused(built, pattern) == ("split.over.glob", "matched no files")


def test_glob_matching_nothing_is_invalid_input(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"a.txt": b"a"})
    assert _refused(built, "*.jpg") == ("split.over.glob", "matched no files")


def test_glob_is_case_sensitive(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"A.JPG": b"1", "b.jpg": b"2"})
    assert list(_resolve(built, "*.jpg").items) == ["b.jpg"]


def test_symlink_to_a_file_inside_the_tree_is_followed(tmp_path: Path) -> None:
    built = _tree(
        tmp_path / "p",
        {"real/x.dat": b"payload"},
        links={"items/link.dat": "../real/x.dat"},
    )
    got = _resolve(built, "items/*")
    assert list(got.items) == ["items/link.dat"]
    assert got.files[0].sha256 == sha256_bytes(b"payload").hex
    assert got.files[0].size == 7


def test_symlink_to_a_folder_is_refused(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"real/x.dat": b"x"}, links={"items/dir": "../real"})
    path, message = _refused(built, "items/*")
    assert path == "split.over.glob" and "folder" in message


def test_symlink_escaping_the_tree_never_becomes_an_item(tmp_path: Path) -> None:
    # build_tree refuses such a project; a hostile *manifest* that slipped
    # through is refused again by the resolver.
    from meshforge.common.tree import TreeEntry

    entries = (TreeEntry("items/evil", "symlink", link="../../etc/passwd"),)
    tree = Tree(
        chunk_size=1 << 20,
        entries=entries,
        tree_id=sha256_bytes(b"t"),
        manifest_bytes=1,
    )
    with pytest.raises(ManifestError) as info:
        resolve(_spec("items/*"), tree, {}, _opener())
    assert "leaves its tree" in info.value.problems[0][1]


def test_symlink_loop_is_refused(tmp_path: Path) -> None:
    from meshforge.common.tree import TreeEntry

    entries = (
        TreeEntry("a", "symlink", link="b"),
        TreeEntry("b", "symlink", link="a"),
    )
    tree = Tree(
        chunk_size=1 << 20,
        entries=entries,
        tree_id=sha256_bytes(b"t"),
        manifest_bytes=1,
    )
    with pytest.raises(ManifestError) as info:
        resolve(_spec("a"), tree, {}, _opener())
    assert "hops" in info.value.problems[0][1]


def test_glob_reaches_into_named_inputs_under_their_mount(tmp_path: Path) -> None:
    project = _tree(tmp_path / "p", {"main.py": b"m", "local/q.jpg": b"q"})
    data = _tree(tmp_path / "d", {"x/1.jpg": b"1", "x/2.jpg": b"2", "readme": b"r"})
    got = _resolve(project, "**/*.jpg", inputs={"in/data": data})
    assert list(got.items) == ["in/data/x/1.jpg", "in/data/x/2.jpg", "local/q.jpg"]
    only_input = _resolve(project, "in/data/x/*.jpg", inputs={"in/data": data})
    assert list(only_input.items) == ["in/data/x/1.jpg", "in/data/x/2.jpg"]


def test_glob_cap_stops_early(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {f"f{i}": b"" for i in range(11)})
    assert _resolve(built, "f*", limits=SplitLimits(max_items=11)).count == 11
    path, message = _refused(built, "f*", limits=SplitLimits(max_items=10))
    assert path == "split.over.glob" and "more than 10 items" in message


def test_glob_matcher_is_linear_with_many_double_stars() -> None:
    parts = glob_parts("/".join(["**"] * 40) + "/x")
    path = "/".join(["a"] * 200)
    started = time.perf_counter()
    assert not glob_match(parts, path)
    assert glob_match(parts, path + "/x")
    assert time.perf_counter() - started < 1.0


def test_may_enter() -> None:
    assert may_enter(glob_parts("**/*.jpg"), "in/data")
    assert may_enter(glob_parts("in/*/x.jpg"), "in/data")
    assert not may_enter(glob_parts("local/*.jpg"), "in/data")
    assert not may_enter(glob_parts("*.jpg"), "in/data/deeper")


# ------------------------------------------------------------------- list


def test_list_keeps_the_written_order(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    got = _resolve(built, {"list": ["zeta", "Alpha", "ünï", "a b"]})
    assert list(got.items) == ["zeta", "Alpha", "ünï", "a b"]
    assert got.item_bytes is None and got.files == ()


def test_list_cap(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    path, _ = _refused(
        built, {"list": ["a", "b", "c"]}, limits=SplitLimits(max_items=2)
    )
    assert path == "split.over.list"


# -------------------------------------------------------------- list_file


def test_list_file_lines_bom_crlf_and_blanks(tmp_path: Path) -> None:
    text = "﻿http://x/1\r\n\r\n  \nsecond item\nthird"
    built = _tree(tmp_path / "p", {"items.txt": text.encode("utf-8")})
    got = _resolve(built, {"list_file": "items.txt"})
    assert list(got.items) == ["http://x/1", "second item", "third"]
    assert got.source is not None and got.source.path == "items.txt"


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"a\nb\na\n", "line 3 repeats"),
        (b"\xff\xfe", "not UTF-8"),
        (b"\n\n  \n", "no items"),
        (b"ok\n" + b"x" * 1025 + b"\n", "line 2 is longer"),
        (b"a\x00b\n", "NUL"),
    ],
)
def test_list_file_refusals(tmp_path: Path, data: bytes, fragment: str) -> None:
    built = _tree(tmp_path / "p", {"items.txt": data})
    path, message = _refused(built, {"list_file": "items.txt"})
    assert path == "split.over.list_file" and fragment in message, message


def test_list_file_must_exist_and_be_a_file(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"dir/x": b"1"})
    assert "no such file" in _refused(built, {"list_file": "nope.txt"})[1]
    assert _refused(built, {"list_file": "dir"}) == (
        "split.over.list_file",
        "is a folder, not a file",
    )


def test_list_file_size_and_item_caps(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"items.txt": b"a\nb\nc\n"})
    assert (
        "larger than"
        in _refused(
            built, {"list_file": "items.txt"}, limits=SplitLimits(max_list_file_bytes=5)
        )[1]
    )
    assert (
        "more than 2"
        in _refused(built, {"list_file": "items.txt"}, limits=SplitLimits(max_items=2))[
            1
        ]
    )


def test_list_file_inside_a_named_input(tmp_path: Path) -> None:
    project = _tree(tmp_path / "p", {"main.py": b""})
    data = _tree(tmp_path / "d", {"ids.txt": b"7\n8\n"})
    got = _resolve(project, {"list_file": "inp/ids.txt"}, inputs={"inp": data})
    assert list(got.items) == ["7", "8"]
    assert (
        "no such file"
        in _refused(project, {"list_file": "inp/missing.txt"}, inputs={"inp": data})[1]
    )


# ------------------------------------------------------------------ range


def test_range_items(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    assert list(_resolve(built, {"range": {"stop": 4}}).items) == [0, 1, 2, 3]
    assert list(
        _resolve(built, {"range": {"start": 5, "stop": -5, "step": -4}}).items
    ) == [5, 1, -3]
    assert _refused(built, {"range": {"start": 3, "stop": 3}}) == (
        "split.over.range",
        "the range is empty",
    )


def test_huge_range_is_refused_without_being_built(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    started = time.perf_counter()
    path, message = _refused(built, {"range": {"stop": 2**53 - 1}})
    assert time.perf_counter() - started < 0.5
    assert path == "split.over.range" and "more than 100000" in message


# ------------------------------------------------------------------ lines


def test_lines_records_header_blanks_and_last_line(tmp_path: Path) -> None:
    data = b"id,v\r\n1,a\r\n\r\n2,b\n   \n3,c"
    built = _tree(tmp_path / "p", {"t.csv": data})
    got = _resolve(built, {"lines": "t.csv", "header": True})
    assert list(got.items) == [0, 1, 2]
    assert got.header == (0, 6)
    assert [data[o : o + n] for o, n in got.records] == [b"1,a\r\n", b"2,b\n", b"3,c"]
    assert got.item_bytes == 5 + 4 + 3
    plain = _resolve(built, {"lines": "t.csv"})
    assert plain.count == 4 and plain.header is None


def test_lines_are_never_decoded(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"t.bin": b"\xff\xfe\n\x00\x01\n"})
    assert _resolve(built, {"lines": "t.bin"}).count == 2


@pytest.mark.parametrize(
    ("data", "header", "fragment"),
    [
        (b"", False, "no records"),
        (b"\n\n", False, "no records"),
        (b"only,header\n", True, "after its header line"),
    ],
)
def test_lines_without_records(
    tmp_path: Path, data: bytes, header: bool, fragment: str
) -> None:
    built = _tree(tmp_path / "p", {"t.csv": data})
    path, message = _refused(built, {"lines": "t.csv", "header": header})
    assert path == "split.over.lines" and fragment in message


def test_lines_long_line_and_caps(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"t.csv": b"short\n" + b"y" * 3_000_000 + b"\nz\n"})
    assert (
        "longer than"
        in _refused(
            built, {"lines": "t.csv"}, limits=SplitLimits(max_record_bytes=1_000_000)
        )[1]
    )
    assert _resolve(built, {"lines": "t.csv"}).count == 3
    many = _tree(tmp_path / "q", {"t.csv": b"r\n" * 50})
    assert (
        "more than 49"
        in _refused(many, {"lines": "t.csv"}, limits=SplitLimits(max_items=49))[1]
    )
    assert (
        "larger than"
        in _refused(
            many, {"lines": "t.csv"}, limits=SplitLimits(max_lines_file_bytes=10)
        )[1]
    )


def test_lines_records_across_read_blocks(tmp_path: Path) -> None:
    rows = [f"{i},{'x' * (i % 97)}\n".encode() for i in range(40_000)]
    data = b"".join(rows)
    assert len(data) > 2 * 1024 * 1024  # several 1 MiB blocks
    built = _tree(tmp_path / "p", {"t.csv": data})
    got = _resolve(built, {"lines": "t.csv"})
    assert got.count == 40_000
    assert all(data[o : o + n] == rows[i] for i, (o, n) in enumerate(got.records))


# ---------------------------------------------------------------- chunks


def test_fixed_chunks_cap(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    ok = _resolve(
        built,
        {"range": {"stop": 100}},
        chunk={"items": 10},
        limits=SplitLimits(max_chunks=10),
    )
    assert ok.count == 100
    with pytest.raises(ManifestError) as info:
        _resolve(
            built,
            {"range": {"stop": 101}},
            chunk={"items": 10},
            limits=SplitLimits(max_chunks=10),
        )
    assert info.value.problems[0][0] == "split.chunk.items"
    assert "11 chunks" in info.value.problems[0][1]


# ------------------------------------------------------------------- blob


def test_blob_is_canonical_json_with_a_stable_digest(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"d/b.x": b"bb", "d/a.x": b"a"})
    got = _resolve(built, "d/*.x")
    doc = json.loads(got.blob())
    assert doc == {
        "v": 1,
        "over": "glob",
        "count": 2,
        "items": ["d/a.x", "d/b.x"],
        "files": [[sha256_bytes(b"a").hex, 1], [sha256_bytes(b"bb").hex, 2]],
    }
    assert got.blob() == json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    assert got.digest == sha256_bytes(got.blob())
    again = _resolve(built, "d/*.x")
    assert again.digest == got.digest


def test_lines_blob_names_its_source_records_and_header(tmp_path: Path) -> None:
    built = _tree(tmp_path / "p", {"t.csv": b"h\n1\n2\n"})
    doc = json.loads(_resolve(built, {"lines": "t.csv", "header": True}).blob())
    assert doc["records"] == [[2, 2], [4, 2]]
    assert doc["header"] == [0, 2]
    assert doc["items"] == [0, 1]
    assert doc["source"]["path"] == "t.csv" and doc["source"]["size"] == 6


def test_limits_are_validated() -> None:
    with pytest.raises(ValueError, match="max_items"):
        SplitLimits(max_items=0)
    with pytest.raises(ValueError, match="max_items"):
        SplitLimits(max_items=100_001)
    with pytest.raises(ValueError, match="max_chunks"):
        SplitLimits(max_chunks=10_001)
