"""V0.10 G4: chunk math, the slice digest, the item-list parser (network input:
refused, never trusted partially) and the parts index."""

from __future__ import annotations

import json

import pytest

from meshforge.common.hashing import sha256_bytes
from meshforge.common.manifest import parse_manifest_data
from meshforge.common.split_chunks import (
    ChunkSpan,
    ItemListError,
    chunk_items_digest,
    chunk_payloads,
    chunk_span,
    parse_item_list,
    parts_index_blob,
)
from meshforge.common.split_items import resolve
from meshforge.common.tree import build_tree

pytestmark = pytest.mark.unit


def test_chunk_payloads_cover_every_item_once_in_order() -> None:
    payloads = chunk_payloads(23, 5)
    assert payloads == [
        {"first": 0, "count": 5},
        {"first": 5, "count": 5},
        {"first": 10, "count": 5},
        {"first": 15, "count": 5},
        {"first": 20, "count": 3},
    ]
    assert chunk_payloads(1, 100) == [{"first": 0, "count": 1}]
    with pytest.raises(ValueError):
        chunk_payloads(0, 1)
    with pytest.raises(ValueError):
        chunk_payloads(100_000, 1)  # 100 000 chunks > 10 000


def test_chunk_span_validates_the_payload() -> None:
    assert chunk_span({"first": 2, "count": 3}, 5) == ChunkSpan(2, 3)
    for bad in ({"first": 3, "count": 3}, {"first": -1, "count": 1}, {"first": True}):
        with pytest.raises(ValueError):
            chunk_span(bad, 5)
    with pytest.raises(TypeError):
        chunk_span([0, 1], 5)


def test_slice_digest_is_canonical_json() -> None:
    assert chunk_items_digest(["a", "é"]) == sha256_bytes('["a","é"]'.encode())
    assert chunk_items_digest([1, -2]) == sha256_bytes(b"[1,-2]")
    assert chunk_items_digest(["a"]) != chunk_items_digest(["a "])


def test_parse_round_trips_g1_item_lists(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "p"
    (root / "d").mkdir(parents=True)
    for name in ("b.txt", "a.txt", "é.txt"):
        (root / "d" / name).write_bytes(name.encode() * 3)
    (root / "t.csv").write_bytes(b"h\n1\n\n2\n3")
    built = build_tree(root)

    def opener(hex_: str):  # type: ignore[no-untyped-def]
        path = next(p for h, p in built.sources.items() if h == hex_)
        return path.open("rb")

    for over in (
        {"glob": "d/*"},
        {"lines": "t.csv", "header": True},
        {"range": {"stop": 4}},
    ):
        manifest = parse_manifest_data(
            {
                "name": "s",
                "entrypoint": "python m.py {items_file}",
                "split": {"over": over},
            }
        )
        assert manifest.split is not None
        items = resolve(manifest.split, built.tree, {}, opener)
        parsed = parse_item_list(items.blob())
        assert parsed.items == items.items
        assert parsed.count == items.count
        if "glob" in over:
            assert [f[0] for f in parsed.files] == [f.sha256 for f in items.files]
        if "lines" in over:
            assert parsed.records == items.records and parsed.header == items.header


@pytest.mark.parametrize(
    "doc",
    [
        {"v": 2, "over": "list", "count": 1, "items": ["a"]},
        {"v": 1, "over": "nope", "count": 1, "items": ["a"]},
        {"v": 1, "over": "list", "count": 2, "items": ["a"]},
        {"v": 1, "over": "list", "count": 1, "items": [""]},
        {"v": 1, "over": "list", "count": 1, "items": ["a\u0000"]},
        {"v": 1, "over": "range", "count": 1, "items": ["1"]},
        {"v": 1, "over": "glob", "count": 1, "items": ["a"], "files": []},
        {"v": 1, "over": "glob", "count": 1, "items": ["a"], "files": [["zz", 1]]},
        {
            "v": 1,
            "over": "lines",
            "count": 1,
            "items": [0],
            "source": {"path": "t", "sha256": "a" * 64, "size": 5},
            "records": [[3, 9]],
            "header": None,
        },
    ],
)
def test_malformed_item_lists_are_refused(doc: dict[str, object]) -> None:
    with pytest.raises(ItemListError):
        parse_item_list(json.dumps(doc).encode())
    with pytest.raises(ItemListError):
        parse_item_list(b"\xff not json")


def test_parts_index_has_no_job_identity() -> None:
    a = parts_index_blob(
        items_digest="sha256:" + "1" * 64,
        chunk_items=2,
        chunks=[(0, 2, "sha256:" + "a" * 64), (2, 1, "sha256:" + "b" * 64)],
    )
    assert json.loads(a) == {
        "v": 1,
        "items_digest": "sha256:" + "1" * 64,
        "chunk_items": 2,
        "chunks": [[0, 2, "sha256:" + "a" * 64], [2, 1, "sha256:" + "b" * 64]],
    }
