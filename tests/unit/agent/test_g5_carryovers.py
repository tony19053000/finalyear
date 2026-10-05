"""V0.5 G5 deliverable 0: G4 reviewer carry-overs 3 and 5.

3. A large tree manifest is hashed and parsed off the event loop (the bound is
   64 MiB by default); a small one stays inline.
5. A peer request cancelled while its read thread runs never closes the file
   under the read, and the file *is* closed once the read finishes (no fd leak).
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas import manifest as manifest_mod
from meshforge.agent.cas.fetcher import BlobFetcher, BlobSource
from meshforge.agent.peer import server as peer_server
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CTL = BlobSource(base_url="http://ctl:1", token="tc")


def _project(tmp_path: Path, files: int) -> BuiltTree:
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(files):
        (root / f"file_{i:05d}_with_a_long_enough_name.txt").write_bytes(b"%d\n" % i)
    return build_tree(root, chunk_size=MIN_CHUNK_SIZE)


async def _fetch_manifest(
    tmp_path: Path, built: BuiltTree, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, bool]]:
    """Fetch the manifest twice (network, then cache); record which thread
    hashed / parsed it."""
    loop_thread = threading.get_ident()
    calls: list[tuple[str, bool]] = []
    real_hash, real_parse = manifest_mod._sha256_hex, manifest_mod.parse_tree

    def hash_(data: Any) -> str:
        calls.append(("hash", threading.get_ident() == loop_thread))
        return real_hash(data)

    def parse(*args: Any, **kwargs: Any) -> Any:
        calls.append(("parse", threading.get_ident() == loop_thread))
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(manifest_mod, "_sha256_hex", hash_)
    monkeypatch.setattr(manifest_mod, "parse_tree", parse)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(built.tree.tree_id.hex)
        return httpx.Response(200, stream=httpx.ByteStream(built.manifest))

    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
    cas.store.prepare()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tree = await BlobFetcher(cas, [CTL], client).tree(built.tree.tree_id)
        assert tree.tree_id == built.tree.tree_id
        cached = BlobFetcher(cas, [CTL], client)
        assert (await cached.tree(built.tree.tree_id)).tree_id == built.tree.tree_id
        assert cached.stats.manifest_hit
    return calls


async def test_a_large_manifest_is_hashed_and_parsed_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _project(tmp_path, 1200)
    assert len(built.manifest) > manifest_mod.INLINE_BYTES
    calls = await _fetch_manifest(tmp_path, built, monkeypatch)
    # network: hash + parse; cache: parse - every one in a worker thread.
    assert [name for name, _ in calls] == ["hash", "parse", "parse"]
    assert not any(on_loop for _, on_loop in calls)


async def test_a_small_manifest_stays_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _project(tmp_path, 3)
    assert len(built.manifest) <= manifest_mod.INLINE_BYTES
    calls = await _fetch_manifest(tmp_path, built, monkeypatch)
    assert calls and all(on_loop for _, on_loop in calls)


async def test_cancel_during_read_owned_closes_the_file_after_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(os.urandom(4096))
    fh = blob.open("rb", buffering=0)
    fd = fh.fileno()
    started, release = threading.Event(), threading.Event()
    real_pread = peer_server._pread

    def slow_pread(f: Any, start: int, length: int) -> bytes:
        started.set()
        assert release.wait(5)
        return real_pread(f, start, length)

    monkeypatch.setattr(peer_server, "_pread", slow_pread)
    owner = peer_server._Owner()
    request = asyncio.ensure_future(peer_server._read_owned(fh, 0, 4096, owner))
    assert await asyncio.to_thread(started.wait, 5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    # The request gave the file to the read thread; it is still open under it.
    assert owner.owns is False
    assert not fh.closed
    os.fstat(fd)  # still a valid descriptor
    release.set()
    for _ in range(200):
        if fh.closed:
            break
        await asyncio.sleep(0.01)
    assert fh.closed
    with pytest.raises(OSError):
        os.fstat(fd)


async def test_a_read_that_fails_after_cancel_still_closes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(b"short")
    fh = blob.open("rb", buffering=0)
    started, release = threading.Event(), threading.Event()
    real_pread = peer_server._pread

    def failing_pread(f: Any, start: int, length: int) -> bytes:
        started.set()
        assert release.wait(5)
        return real_pread(f, start, length)  # asks past the end -> ConnectionError

    monkeypatch.setattr(peer_server, "_pread", failing_pread)
    request = asyncio.ensure_future(
        peer_server._read_owned(fh, 0, 4096, peer_server._Owner())
    )
    assert await asyncio.to_thread(started.wait, 5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    for _ in range(200):
        if fh.closed:
            break
        await asyncio.sleep(0.01)
    assert fh.closed
