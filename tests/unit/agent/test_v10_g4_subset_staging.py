"""V0.10 G4 (D264): a lane stages the project WITHOUT the split item files and
each chunk fetches + places only its own (``stage_tree(skip=...)``,
``stage_entries``, ``materialise_subset`` / ``remove_subset``)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from meshforge.agent.cas import AgentCas, BlobSource
from meshforge.agent.cas.materialise import (
    MaterialiseError,
    materialise_subset,
    remove_subset,
)
from meshforge.agent.execution.fetch import FetchError, stage_entries, stage_tree
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = pytest.mark.unit

CS = MIN_CHUNK_SIZE
POSIX = not sys.platform.startswith("win")
SOURCE = BlobSource(base_url="http://ctl:1", token="tok")


def _cas(tmp_path: Path) -> AgentCas:
    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
    cas.store.prepare()
    return cas


def _project(tmp_path: Path, n: int = 6) -> BuiltTree:
    root = tmp_path / "proj"
    (root / "images").mkdir(parents=True)
    (root / "main.py").write_bytes(b"print(1)\n")
    for i in range(n):
        (root / "images" / f"{i}.bin").write_bytes(os.urandom(100 + i))
    return build_tree(root, chunk_size=CS)


class _Server:
    def __init__(self, built: BuiltTree) -> None:
        self.blobs = {h: p.read_bytes() for h, p in built.sources.items()}
        self.blobs[built.tree.tree_id.hex] = built.manifest
        self.asked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        hex_ = request.url.path.rsplit("/", 1)[-1]
        data = self.blobs.get(hex_)
        if data is None:
            return httpx.Response(404)
        self.asked.append(hex_)
        rng = request.headers.get("range")
        status = 200
        if rng:
            a, b = rng.removeprefix("bytes=").split("-")
            data, status = data[int(a) : int(b) + 1], 206
        return httpx.Response(
            status,
            headers={"content-length": str(len(data))},
            stream=httpx.ByteStream(data),
        )


@pytest.mark.asyncio
async def test_lane_staging_skips_the_items_and_chunks_place_only_theirs(
    tmp_path: Path,
) -> None:
    built = _project(tmp_path)
    server = _Server(built)
    cas = _cas(tmp_path)
    transport = httpx.MockTransport(server.handler)
    items = frozenset(f"images/{i}.bin" for i in range(6))
    work = tmp_path / "work"
    report = await stage_tree(
        cas,
        tree_id=str(built.tree.tree_id),
        sources=[SOURCE],
        dest=work,
        transport=transport,
        skip=lambda _tree: items,
    )
    assert report.skipped == items
    assert (work / "main.py").exists()
    assert not (work / "images").exists()  # D264: no item staged (folder implied)
    item_hexes = {e.sha256 for e in report.tree.files() if e.path in items}
    assert not item_hexes & set(server.asked)  # never fetched for the lane
    # chunk 1: two of the items
    got = await stage_entries(
        cas,
        tree=report.tree,
        paths=["images/1.bin", "images/4.bin"],
        sources=[SOURCE],
        dest=work,
        transport=transport,
    )
    assert sorted(p.name for p in got.placed) == ["1.bin", "4.bin"]
    assert sorted(os.listdir(work / "images")) == ["1.bin", "4.bin"]
    root = tmp_path / "proj"
    assert (work / "images" / "4.bin").read_bytes() == (
        root / "images" / "4.bin"
    ).read_bytes()
    if POSIX:
        assert stat.S_IMODE((work / "images" / "1.bin").stat().st_mode) == 0o444
    assert got.blobs_fetched == 2
    assert remove_subset(list(got.placed), work) == 2
    assert os.listdir(work / "images") == []
    # the same chunk again on this node: nothing is fetched (cached)
    again = await stage_entries(
        cas,
        tree=report.tree,
        paths=["images/1.bin"],
        sources=[SOURCE],
        dest=work,
        transport=transport,
    )
    assert again.blobs_fetched == 0 and again.blobs_hit == 1


@pytest.mark.asyncio
async def test_a_path_that_is_not_a_file_of_the_tree_is_refused(
    tmp_path: Path,
) -> None:
    built = _project(tmp_path, n=1)
    cas = _cas(tmp_path)
    with pytest.raises(FetchError) as exc:
        await stage_entries(
            cas,
            tree=built.tree,
            paths=["../etc/passwd"],
            sources=[SOURCE],
            dest=tmp_path,
            transport=httpx.MockTransport(_Server(built).handler),
        )
    assert exc.value.failure_type.value == "invalid_input"


@pytest.mark.skipif(not POSIX, reason="read-only folders and symlinks: POSIX")
def test_materialise_subset_refuses_a_taken_path_and_cleans_up(
    tmp_path: Path,
) -> None:
    built = _project(tmp_path, n=2)
    cas = _cas(tmp_path)
    for path in built.sources.values():
        cas.store.put_file(path)
    entries = [e for e in built.tree.files() if e.path.startswith("images/")]
    work = tmp_path / "work"
    (work / "images").mkdir(parents=True)
    # the chunk's job left a file in the way of the second item
    (work / "images" / "1.bin").write_bytes(b"theirs")
    with pytest.raises(MaterialiseError):
        materialise_subset(cas.store, entries, work)
    assert os.listdir(work / "images") == ["1.bin"]  # the first was removed again
    assert (work / "images" / "1.bin").read_bytes() == b"theirs"  # never overwritten


@pytest.mark.skipif(not POSIX, reason="read-only folders: POSIX")
def test_materialise_subset_into_a_read_only_input_folder(tmp_path: Path) -> None:
    built = _project(tmp_path, n=1)
    cas = _cas(tmp_path)
    for path in built.sources.values():
        cas.store.put_file(path)
    entry = next(e for e in built.tree.files() if e.path == "images/0.bin")
    work = tmp_path / "work"
    (work / "images").mkdir(parents=True)
    (work / "images").chmod(0o555)  # a named input folder is read-only
    placed = materialise_subset(cas.store, [entry], work)
    assert placed == [work / "images" / "0.bin"]
    assert stat.S_IMODE((work / "images").stat().st_mode) == 0o555  # restored
    remove_subset(placed, work)
    assert os.listdir(work / "images") == []
