"""V0.10 G5 (D268/D269/D283): the pure pieces of the combine - the parts index
parser, job warnings, the combine's manifest and fetch scope, plain-blob fetch
grants."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.manifest import Manifest
from meshforge.common.split_chunks import (
    ItemListError,
    parse_parts_index,
    parts_index_blob,
)
from meshforge.common.states import JobState
from meshforge.controller.blobs import MAX_FETCH_TOKEN_BLOBS, FetchTokens
from meshforge.controller.jobs.dispatch import CombineScope, fetch_scope
from meshforge.controller.jobs.spec import combine_manifest
from meshforge.controller.jobs.store import JobRecord, SplitRecord
from meshforge.controller.split.warnings import (
    MAX_CHUNKS_LISTED,
    NONDETERMINISTIC_CHUNK,
    add_chunk_warning,
    parse_warnings,
)

pytestmark = [pytest.mark.unit]

D = "sha256:" + "a" * 64
E = "sha256:" + "b" * 64


# ------------------------------------------------------------------ parts index


def test_parts_index_round_trip() -> None:
    blob = parts_index_blob(
        items_digest=D, chunk_items=10, chunks=[(0, 10, D), (10, 10, E), (20, 3, D)]
    )
    index = parse_parts_index(blob)
    assert index.items_digest == D
    assert index.chunk_items == 10
    assert index.digests == (D, E, D)


@pytest.mark.parametrize(
    "doc",
    [
        b"not json",
        b'{"v": 2, "items_digest": "%s", "chunk_items": 1, "chunks": [[0,1,"%s"]]}'
        % (D.encode(), D.encode()),
        b'{"v": 1, "items_digest": "%s", "chunk_items": 1, "chunks": []}' % D.encode(),
        b'{"v": 1, "items_digest": "%s", "chunk_items": 1, "chunks": [[1,1,"%s"]]}'
        % (D.encode(), D.encode()),  # does not start at item 0
        b'{"v": 1, "items_digest": "%s", "chunk_items": 1, '
        b'"chunks": [[0,1,"%s"],[2,1,"%s"]]}' % (D.encode(), D.encode(), D.encode()),
        b'{"v": 1, "items_digest": "%s", "chunk_items": 1, "chunks": [[0,1,"x"]]}'
        % D.encode(),
        b'{"v": 1, "items_digest": "%s", "chunk_items": 1, "chunks": [[0,0,"%s"]]}'
        % (D.encode(), D.encode()),
        b'{"v": 1, "items_digest": "nope", "chunk_items": 1, "chunks": [[0,1,"%s"]]}'
        % D.encode(),
    ],
)
def test_parts_index_refuses_malformed(doc: bytes) -> None:
    with pytest.raises(ItemListError):
        parse_parts_index(doc)


# ------------------------------------------------------------------ warnings


def test_warning_aggregates_per_code_and_bounds_its_chunk_list() -> None:
    raw: Any = None
    raw, fresh = add_chunk_warning(raw, NONDETERMINISTIC_CHUNK, 7)
    assert fresh
    raw, fresh = add_chunk_warning(raw, NONDETERMINISTIC_CHUNK, 7)
    assert not fresh
    raw, _ = add_chunk_warning(raw, NONDETERMINISTIC_CHUNK, 3)
    (entry,) = raw
    assert entry["code"] == NONDETERMINISTIC_CHUNK
    assert entry["count"] == 3
    assert entry["chunks"] == [3, 7]
    for k in range(1000):
        raw, _ = add_chunk_warning(raw, NONDETERMINISTIC_CHUNK, 100 + k)
    (entry,) = raw
    assert entry["count"] == 1003
    assert len(entry["chunks"]) == MAX_CHUNKS_LISTED


def test_parse_warnings_drops_what_it_does_not_know() -> None:
    raw = [
        {"code": "nondeterministic_chunk", "count": 2, "chunks": [5, -1, "x", True, 2]},
        {"code": "made_up", "count": 1, "chunks": []},
        "junk",
    ]
    (entry,) = parse_warnings(raw)
    assert entry["chunks"] == [2, 5]
    assert entry["detail"]
    assert parse_warnings({"not": "a list"}) == []
    assert parse_warnings([{"code": "nondeterministic_chunk", "count": 0}]) == []
    with pytest.raises(ValueError):
        add_chunk_warning(None, "made_up", 1)


# ------------------------------------------------------------------ manifest


def test_combine_manifest_drops_only_the_gpu_need() -> None:
    manifest = Manifest.model_validate(
        {
            "name": "x",
            "entrypoint": "python main.py {items_file} {out}",
            "split": {"over": {"range": {"stop": 10}}},
            "resources": {"gpu": "required", "vram_gb": 8, "ram_gb": 2, "cpus": 2},
            "limits": {"timeout": 123},
        }
    )
    combined = combine_manifest(manifest)
    assert combined.resources.gpu == "none"
    assert combined.resources.vram_gb is None
    assert combined.resources.ram_gb == 2
    assert combined.resources.cpus == 2
    assert combined.limits.timeout == 123
    assert combined.split == manifest.split
    # it is still a manifest the agent accepts as is
    again = Manifest.model_validate(combined.canonical_dict())
    assert again.resources.gpu == "none"


# ------------------------------------------------------------------ fetch scope


def _job(**split: Any) -> JobRecord:
    now = dt.datetime.now(dt.UTC)
    return JobRecord(
        id="job_x",
        name="x",
        state=JobState.COLLECTING,
        manifest={},
        bundle_digest=None,
        placement={},
        idempotency_key=None,
        request_hash="0" * 64,
        attempt_epoch=3,
        failure_type=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
        tree_id=D,
        split=SplitRecord(spec=split, items_digest=E, item_count=10, parts_digest=E),
    )


def test_a_builtin_combine_reads_only_its_parts() -> None:
    scope = CombineScope(E, (D, D), script=False)
    assert fetch_scope(_job(), [E], combine=scope) == []
    assert scope.blobs == [Digest.parse(E), Digest.parse(D), Digest.parse(D)]


def test_a_script_combine_also_reads_the_project_and_inputs() -> None:
    scope = CombineScope(E, (D,), script=True)
    got = fetch_scope(_job(), ["sha256:" + "c" * 64], combine=scope)
    assert got == [Digest.parse(D), Digest.parse("sha256:" + "c" * 64)]


def test_plain_blob_grants_hold_every_part_and_are_not_roots() -> None:
    tokens = FetchTokens(clock=lambda: 100.0)
    parts = [sha256_bytes(str(i).encode()) for i in range(MAX_FETCH_TOKEN_BLOBS)]
    secret = tokens.mint("att_c", [], ttl_s=60, blobs=parts).get_secret_value()
    grant = tokens.grant(secret)
    assert grant is not None
    assert grant.digests == frozenset()  # nothing walked as a tree
    assert all(tokens.check(secret, p) for p in parts[:50])
    assert not tokens.check(secret, sha256_bytes(b"other"))
    tokens.expire_after("att_c", 1.0)
    regrant = tokens.grant(secret)
    assert regrant is not None and len(regrant.blobs) == MAX_FETCH_TOKEN_BLOBS
    with pytest.raises(ValueError):
        tokens.mint(
            "att_c",
            [],
            ttl_s=60,
            blobs=[*parts, sha256_bytes(b"one too many")],
        )
