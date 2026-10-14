"""V0.5 G1 D81: deterministic tree manifests (``common/tree.py``)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.tree import (
    TreeError,
    TreeLimits,
    build_tree,
    parse_tree,
)

WINDOWS = sys.platform.startswith("win")
posix_only = pytest.mark.skipif(WINDOWS, reason="needs POSIX symlinks/exec bits")
CS = MIN_CHUNK_SIZE

GOLDEN_TREE_ID = "a469c867a598ec3236c4da3486e7ac477e02a0f900ce63d45984d1ed36d62c29"
"""Pinned for the fixed tree below. Nothing OS-specific goes into a manifest, so
CI must get the same id on windows-latest. A change here changes every tree id."""


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _golden(root: Path) -> Path:
    root.mkdir()
    _write(root, "main.py", b"print('hello')\n")
    _write(root, "pkg/__init__.py", b"")
    _write(root, "pkg/data.bin", bytes(range(256)) * 600)  # 153600 B > CS: chunked
    _write(root, "zé/café.txt", "café\n".encode())
    _write(root, ".gitignore", b"*.log\n")
    _write(root, "debug.log", b"ignored")
    (root / "empty").mkdir()
    return root


def test_golden_tree_id_is_fixed(tmp_path: Path) -> None:
    built = build_tree(_golden(tmp_path / "proj"), chunk_size=CS)
    assert str(built.tree.tree_id) == f"sha256:{GOLDEN_TREE_ID}", built.manifest
    paths = [e.path for e in built.tree.entries]
    assert paths == [
        ".gitignore",
        "empty",
        "main.py",
        "pkg/__init__.py",
        "pkg/data.bin",
        "zé/café.txt",
    ]
    data = next(e for e in built.tree.entries if e.path == "pkg/data.bin")
    assert len(data.chunks) == 3 and data.size == 153600
    assert built.ignored == 1


def test_manifest_is_canonical_json_and_hashes_to_the_id(tmp_path: Path) -> None:
    built = build_tree(_golden(tmp_path / "proj"), chunk_size=CS)
    assert hashlib.sha256(built.manifest).hexdigest() == built.tree.tree_id.hex
    body = json.loads(built.manifest)
    assert set(body) == {"v", "chunk_size", "entries"} and body["v"] == 1
    assert b" " not in built.manifest.replace(b"print('hello')", b"")
    assert parse_tree(built.manifest, expected=built.tree.tree_id) == built.tree


def test_same_content_same_id_regardless_of_mtime_and_creation_order(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a"
    a.mkdir()
    _write(a, "x.txt", b"1")
    _write(a, "y.txt", b"2")
    b = tmp_path / "b"
    b.mkdir()
    _write(b, "y.txt", b"2")
    _write(b, "x.txt", b"1")
    os.utime(b / "x.txt", (12345, 12345))
    assert build_tree(a).tree.tree_id == build_tree(b).tree.tree_id


def test_one_changed_file_changes_only_its_entry(tmp_path: Path) -> None:
    root = _golden(tmp_path / "proj")
    before = build_tree(root, chunk_size=CS).tree
    _write(root, "main.py", b"print('changed')\n")
    after = build_tree(root, chunk_size=CS).tree
    assert before.tree_id != after.tree_id
    changed = set(after.blob_sizes()) - set(before.blob_sizes())
    assert len(changed) == 1


@posix_only
def test_exec_bit_changes_the_id(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root, "run.sh", b"#!/bin/sh\n")
    first = build_tree(root).tree.tree_id
    (root / "run.sh").chmod(0o755)
    assert build_tree(root).tree.tree_id != first


@pytest.mark.security
@posix_only
def test_symlinks_inside_are_kept_and_escaping_ones_refused(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root, "real.txt", b"r")
    (root / "link.txt").symlink_to("real.txt")
    tree = build_tree(root).tree
    link = next(e for e in tree.entries if e.path == "link.txt")
    assert link.kind == "symlink" and link.link == "real.txt"
    (root / "bad").symlink_to("../outside")
    with pytest.raises(TreeError):
        build_tree(root)


def test_build_bounds_entries_max_and_max_plus_one(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    for i in range(3):
        _write(root, f"f{i}", b"x")
    assert len(build_tree(root, limits=TreeLimits(max_entries=3)).tree.entries) == 3
    with pytest.raises(TreeError):
        build_tree(root, limits=TreeLimits(max_entries=2))


def test_build_bounds_total_bytes_max_and_max_plus_one(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root, "f", b"x" * 10)
    build_tree(root, limits=TreeLimits(max_total_bytes=10))
    with pytest.raises(TreeError):
        build_tree(root, limits=TreeLimits(max_total_bytes=9))


def test_build_bounds_path_bytes_max_and_max_plus_one(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write(root, "abcd", b"x")
    build_tree(root, limits=TreeLimits(max_path_bytes=4))
    with pytest.raises(TreeError):
        build_tree(root, limits=TreeLimits(max_path_bytes=3))


# ----------------------------------------------------------------- parsing


def _manifest(entries: list[dict[str, Any]], chunk_size: int = CS) -> bytes:
    return json.dumps(
        {"v": 1, "chunk_size": chunk_size, "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


H = "a" * 64


def _file(path: str, size: int = 1, **extra: Any) -> dict[str, Any]:
    return {"path": path, "sha256": H, "size": size, "exec": False, **extra}


def test_parse_accepts_a_valid_manifest() -> None:
    tree = parse_tree(
        _manifest(
            [
                _file("a"),
                {"path": "d", "dir": True},
                {"path": "l", "link": "a"},
                _file("z", CS + 1, chunks=[H, H]),
            ]
        )
    )
    assert [e.kind for e in tree.entries] == ["file", "dir", "symlink", "file"]


@pytest.mark.security
@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"\xff", "not utf-8"),
        (b"[]", "not an object"),
        (_manifest([_file("a")]).replace(b'"v":1', b'"v":2'), "version"),
        (_manifest([_file("a")], chunk_size=CS - 1), "chunk size"),
        (_manifest([_file("b"), _file("a")]), "unsorted"),
        (_manifest([_file("a"), _file("a")]), "duplicate"),
        (_manifest([_file("../x")]), "dotdot"),
        (_manifest([_file("/etc/passwd")]), "absolute"),
        (_manifest([_file("a\\b")]), "backslash"),
        (_manifest([_file("a/")]), "trailing slash"),
        (_manifest([_file("a"), _file("a/b")]), "under a file"),
        (_manifest([{"path": "d", "dir": True}, _file("d/x")]), "under empty dir"),
        (_manifest([{"path": "l", "link": "../../x"}]), "escaping link"),
        (_manifest([{"path": "l", "link": "/abs"}]), "absolute link"),
        (_manifest([{"path": "d", "dir": False}]), "dir false"),
        (_manifest([_file("a", sha256="A" * 64)]), "upper hex"),
        (_manifest([_file("a", size=-1)]), "negative size"),
        (_manifest([_file("a", size=True)]), "bool size"),
        (_manifest([_file("a", exec=1)]), "exec not bool"),
        (_manifest([_file("a", CS + 1, chunks=[H])]), "chunk count"),
        (_manifest([_file("a", CS + 1)]), "missing chunks"),
        (_manifest([_file("a", 1, chunks=[H])]), "chunks on one-chunk file"),
        (_manifest([_file("a", CS + 1, chunks=[H, "x"])]), "bad chunk hex"),
        (_manifest([{**_file("a"), "mode": 420}]), "extra key"),
        (_manifest([_file("a")]).replace(b":", b": ", 1), "non-canonical"),
    ],
)
def test_parse_refuses_hostile_manifests(data: bytes, why: str) -> None:
    with pytest.raises(TreeError) as info:
        parse_tree(data)
    assert info.value.failure_type is FailureType.INVALID_INPUT, why


@pytest.mark.security
def test_parse_checks_the_expected_id() -> None:
    data = _manifest([_file("a")])
    with pytest.raises(TreeError) as info:
        parse_tree(data, expected=Digest("b" * 64))
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR


def test_parse_bounds_manifest_bytes_max_and_max_plus_one() -> None:
    data = _manifest([_file("a")])
    parse_tree(data, limits=TreeLimits(max_manifest_bytes=len(data)))
    with pytest.raises(TreeError):
        parse_tree(data, limits=TreeLimits(max_manifest_bytes=len(data) - 1))


def test_parse_bounds_entries_max_and_max_plus_one() -> None:
    data = _manifest([_file("a"), _file("b")])
    parse_tree(data, limits=TreeLimits(max_entries=2))
    with pytest.raises(TreeError):
        parse_tree(data, limits=TreeLimits(max_entries=1))


def test_parse_bounds_total_and_path_max_and_max_plus_one() -> None:
    data = _manifest([_file("abc", 5), _file("abd", 5)])
    parse_tree(data, limits=TreeLimits(max_total_bytes=10, max_path_bytes=3))
    with pytest.raises(TreeError):
        parse_tree(data, limits=TreeLimits(max_total_bytes=9))
    with pytest.raises(TreeError):
        parse_tree(data, limits=TreeLimits(max_path_bytes=2))
