"""V0.5 G1 tester: tree_id stability and hostile manifests, derived from D81."""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import Digest
from meshforge.common.tree import (
    TreeError,
    TreeLimits,
    build_tree,
    canonical_bytes,
    parse_tree,
)

CS = MIN_CHUNK_SIZE
H = "a" * 64
posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX only")


def _w(root: Path, rel: str, data: bytes) -> None:
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _m(entries: list[dict[str, Any]], cs: int = CS) -> bytes:
    return json.dumps(
        {"v": 1, "chunk_size": cs, "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _f(path: str, size: int = 1, **kw: Any) -> dict[str, Any]:
    return {"path": path, "sha256": H, "size": size, "exec": False, **kw}


def test_tree_id_independent_of_creation_order_many_files(tmp_path: Path) -> None:
    names = [f"d{i % 3}/f{i}.txt" for i in range(40)] + ["Z", "a", "_", "é"]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for n in names:
        _w(a, n, n.encode())
    for n in reversed(names):
        _w(b, n, n.encode())
    assert build_tree(a).tree.tree_id == build_tree(b).tree.tree_id


def test_chunk_size_is_part_of_the_id(tmp_path: Path) -> None:
    _w(tmp_path, "f", b"x")
    assert (
        build_tree(tmp_path, chunk_size=CS).tree.tree_id
        != build_tree(tmp_path, chunk_size=2 * CS).tree.tree_id
    )


def test_crlf_and_lf_files_are_different_blobs(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _w(a, "x.txt", b"l1\nl2\n")
    _w(b, "x.txt", b"l1\r\nl2\r\n")
    assert (
        build_tree(a).tree.tree_id != build_tree(b).tree.tree_id
    )  # no newline munging


def test_empty_file_and_empty_tree(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    empty = build_tree(root)
    assert empty.tree.entries == ()
    assert parse_tree(empty.manifest, expected=empty.tree.tree_id).entries == ()
    _w(root, "z", b"")
    built = build_tree(root)
    (e,) = built.tree.entries
    assert e.size == 0 and e.chunks == ()
    assert parse_tree(built.manifest) == built.tree


def test_exact_chunk_boundary_sizes(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _w(root, "one", b"a" * CS)  # exactly one chunk: no chunk list
    _w(root, "two", b"b" * (CS + 1))
    _w(root, "twoexact", b"c" * (2 * CS))
    built = build_tree(root, chunk_size=CS)
    by = {e.path: e for e in built.tree.entries}
    assert by["one"].chunks == ()
    assert len(by["two"].chunks) == 2 and len(by["twoexact"].chunks) == 2
    assert parse_tree(built.manifest) == built.tree


def test_unicode_nfc_and_nfd_names_never_collapse_in_the_manifest(
    tmp_path: Path,
) -> None:
    """Names are bytes-exact: NFC and NFD spellings are two entries (a filesystem
    that normalises, like macOS, would hold one - the id must still be stable)."""
    nfc, nfd = (
        unicodedata.normalize("NFC", "é.txt"),
        unicodedata.normalize("NFD", "é.txt"),
    )
    assert nfc != nfd
    entries = [_f(nfc), _f(nfd)]
    entries.sort(key=lambda e: e["path"].encode())
    tree = parse_tree(_m(entries))
    assert len(tree.entries) == 2


@posix_only
def test_unicode_names_build_and_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    for n in ("日本語/ファイル.txt", "emoji-\U0001f600.txt", "e\u0301.txt", "é.txt"):
        _w(root, n, n.encode())
    built = build_tree(root)
    again = build_tree(root)
    assert built.tree.tree_id == again.tree.tree_id
    assert parse_tree(built.manifest, expected=built.tree.tree_id) == built.tree


@posix_only
def test_case_only_differences_are_distinct_entries(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _w(root, "Readme.md", b"1")
    _w(root, "README.md", b"2")
    built = build_tree(root)
    assert {e.path for e in built.tree.entries} == {"Readme.md", "README.md"}
    assert parse_tree(built.manifest) == built.tree


@posix_only
def test_non_utf8_filename_is_refused_not_silently_mangled(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    import os

    fd = os.open(os.fsencode(root) + b"/bad\xff.txt", os.O_CREAT | os.O_WRONLY)
    os.close(fd)
    with pytest.raises(TreeError):
        build_tree(root)


@posix_only
def test_symlink_to_directory_and_dangling_and_chain(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _w(root, "d/x.txt", b"x")
    (root / "dl").symlink_to("d")
    (root / "dangling").symlink_to("nowhere")
    (root / "chain").symlink_to("dl")
    built = build_tree(root)
    kinds = {e.path: e.kind for e in built.tree.entries}
    assert kinds["dl"] == "symlink" and kinds["dangling"] == "symlink"
    # Nothing is listed *through* a symlink to a folder.
    assert "dl/x.txt" not in kinds
    assert parse_tree(built.manifest) == built.tree


@posix_only
def test_symlink_absolute_target_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "l").symlink_to("/etc/passwd")
    with pytest.raises(TreeError):
        build_tree(root)


@posix_only
def test_empty_dir_nested_chain_listed_once(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / "a" / "b" / "c").mkdir(parents=True)
    built = build_tree(root)
    assert [e.path for e in built.tree.entries] == ["a/b/c"]
    _w(root, "a/f", b"x")
    built = build_tree(root)
    assert [e.path for e in built.tree.entries] == ["a/b/c", "a/f"]


@posix_only
def test_exec_bit_on_symlink_target_and_dir_modes_do_not_leak(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for r in (a, b):
        r.mkdir()
        _w(r, "d/f", b"x")
    (b / "d").chmod(0o700)
    (b / "d" / "f").chmod(0o644)
    try:
        assert build_tree(a).tree.tree_id == build_tree(b).tree.tree_id
    finally:
        (b / "d").chmod(0o755)


@pytest.mark.parametrize(
    "bad",
    [
        "./a",
        "a/./b",
        "a//b",
        "a/../b",
        "..",
        ".",
        "",
        "a\x00b",
        "C:/x",
        "C:\\x",
        "//server/share",
        "\ud800",  # lone surrogate
        "a/",
        "/a",
        "con\\x",
    ],
)
def test_parse_refuses_more_hostile_paths(bad: str) -> None:
    try:
        data = _m([_f(bad)])
    except UnicodeEncodeError:
        data = json.dumps(
            {"v": 1, "chunk_size": CS, "entries": [_f(bad)]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    with pytest.raises(TreeError):
        parse_tree(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(size=2**63),  # beyond the total cap
        lambda e: e.update(size=1.0),
        lambda e: e.update(size="1"),
        lambda e: e.update(sha256=None),
        lambda e: e.update(sha256="sha256:" + H),
        lambda e: e.update(path=5),
        lambda e: e.update(exec="false"),
    ],
)
def test_parse_refuses_bad_field_types(mutate: Any) -> None:
    e = _f("a")
    mutate(e)
    with pytest.raises(TreeError):
        parse_tree(_m([e]))


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"1",
        b'"x"',
        b"{}",
        b'{"v":1,"chunk_size":65536,"entries":{}}',
        b'{"v":1,"chunk_size":65536,"entries":[null]}',
        b'{"v":1,"chunk_size":65536,"entries":[[]]}',
        b'{"v":true,"chunk_size":65536,"entries":[]}',
        b'{"v":1,"chunk_size":true,"entries":[]}',
        b'{"v":1,"chunk_size":65536.0,"entries":[]}',
        b'{"v":1,"chunk_size":65536,"entries":[],"extra":1}',
        b"[" * 100000,
        b'{"v":1,"chunk_size":65536,"entries":[]}\n',
        b'\xef\xbb\xbf{"v":1,"chunk_size":65536,"entries":[]}',
        b"",
    ],
)
def test_parse_refuses_garbage_documents(raw: bytes) -> None:
    with pytest.raises(TreeError):
        parse_tree(raw)


def test_chunk_list_wrong_length_both_directions() -> None:
    n = 3
    size = 2 * CS + 1
    assert len(parse_tree(_m([_f("a", size, chunks=[H] * n)])).entries) == 1
    for count in (n - 1, n + 1, 0):
        with pytest.raises(TreeError):
            parse_tree(_m([_f("a", size, chunks=[H] * count)]))
    # size exactly at the boundary: 2 chunks, not 3
    parse_tree(_m([_f("a", 2 * CS, chunks=[H] * 2)]))
    with pytest.raises(TreeError):
        parse_tree(_m([_f("a", 2 * CS, chunks=[H] * 3)]))


def test_entries_max_exact_and_plus_one_default_limits_and_big_manifest() -> None:
    lim = TreeLimits(max_entries=1000)
    ok = _m([_f(f"f{i:05d}") for i in range(1000)])
    assert len(parse_tree(ok, limits=lim).entries) == 1000
    with pytest.raises(TreeError):
        parse_tree(_m([_f(f"f{i:05d}") for i in range(1001)]), limits=lim)


def test_symlink_that_is_a_prefix_of_a_later_entry_is_refused() -> None:
    with pytest.raises(TreeError):
        parse_tree(_m([{"path": "l", "link": "x"}, _f("l/y")]))


def test_symlink_target_tricks() -> None:
    for target in ("../../x", "a/../../../x", "../..", "x\\y", "", "\x00"):
        with pytest.raises(TreeError):
            parse_tree(_m([{"path": "d/l", "link": target}]))
    parse_tree(_m([{"path": "d/l", "link": "../ok"}]))  # stays inside


def test_canonical_bytes_roundtrip_is_the_only_accepted_form() -> None:
    t = parse_tree(_m([_f("a"), _f("b")]))
    assert canonical_bytes(t.chunk_size, t.entries) == _m([_f("a"), _f("b")])
    # Same content, ascii-escaped unicode: different bytes, refused.
    nonascii = _m([_f("é")])
    escaped = nonascii.replace("é".encode(), b"\\u00e9")
    with pytest.raises(TreeError):
        parse_tree(escaped)


def test_expected_id_mismatch_is_integrity_error_before_parse() -> None:
    from meshforge.common.errors import FailureType

    with pytest.raises(TreeError) as info:
        parse_tree(b"not json", expected=Digest("c" * 64))
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
