"""V0.6 G1 (D119, V0.5 G5 reviewer): the controller's GC-index writes run off
the event loop.

``ControllerGc._on_change`` is the CAS observer: every blob the controller's
CAS stores or removes writes one SQLite row (``BEGIN IMMEDIATE``, up to a 30 s
busy timeout). It runs in whatever thread changed the CAS - so every path that
changes the CAS must do so in a worker thread, never on the loop. This test
drives each real path (streamed PUT, resumable parts + complete, a GC eviction,
a scrub eviction, a manifest-mismatch eviction on read) through a live
controller and records the thread of every index write.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_node_id
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,  # type: ignore[arg-type]
        blobs={"max_blob_bytes": 16 * 1024 * 1024},
        gc={"cas_bytes": 1, "min_age_s": 0, "interval_s": 3600},
    )
    async with running_controller(settings) as rt:
        yield rt


class _Threads:
    """Which thread ran each index write (``upsert`` / ``remove``)."""

    def __init__(self, loop_thread: int) -> None:
        self.loop_thread = loop_thread
        self.calls: list[tuple[str, str, bool]] = []

    def wrap(self, name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def recorded(kind: str, key: str, *args: Any, **kwargs: Any) -> Any:
            on_loop = threading.get_ident() == self.loop_thread
            self.calls.append((name, key, on_loop))
            return real(kind, key, *args, **kwargs)

        return recorded

    def for_key(self, name: str, key: str) -> list[bool]:
        return [on for n, k, on in self.calls if n == name and k == key]


def _part_headers(size: int, chunk: bytes) -> dict[str, str]:
    return {
        "X-MeshForge-Blob-Size": str(size),
        "X-MeshForge-Chunk-Size": str(CS),
        "X-MeshForge-Chunk-Sha256": hashlib.sha256(chunk).hexdigest(),
    }


async def test_every_cas_change_writes_the_gc_index_off_the_event_loop(
    runtime: ControllerRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runtime.gc is not None and runtime.blobs is not None
    assert runtime.scrubber is not None
    index = runtime.gc.index
    threads = _Threads(threading.get_ident())  # the test runs on the loop thread
    monkeypatch.setattr(index, "upsert", threads.wrap("upsert", index.upsert))
    monkeypatch.setattr(index, "remove", threads.wrap("remove", index.remove))

    streamed = b"streamed upload\n" * 100
    parted = os.urandom(2 * CS + 7)
    corrupt = b"rots on disk\n"
    async with api_client(runtime, token=token_of(runtime)) as client:
        # 1. a streamed PUT
        put = await client.put(
            f"/v1/blobs/{sha256_bytes(streamed).hex}", content=streamed
        )
        assert put.status_code == 201, put.text
        # 2. resumable parts + complete
        digest = sha256_bytes(parted)
        for i, start in enumerate(range(0, len(parted), CS)):
            chunk = parted[start : start + CS]
            got = await client.put(
                f"/v1/blobs/{digest.hex}/parts/{i}",
                content=chunk,
                headers=_part_headers(len(parted), chunk),
            )
            assert got.status_code == 201, got.text
        done = await client.post(f"/v1/blobs/{digest.hex}/complete")
        assert done.status_code == 201, done.text
        bad = await client.put(
            f"/v1/blobs/{sha256_bytes(corrupt).hex}", content=corrupt
        )
        assert bad.status_code == 201
    assert threads.for_key("upsert", sha256_bytes(streamed).hex) == [False]
    assert threads.for_key("upsert", digest.hex) == [False]

    # 3. a scrub eviction (same-size corruption on disk)
    path = runtime.blobs.path_for(sha256_bytes(corrupt))
    path.chmod(0o600)
    path.write_bytes(corrupt.upper())
    assert await runtime.scrubber.check(sha256_bytes(corrupt)) is False
    assert threads.for_key("remove", sha256_bytes(corrupt).hex)
    assert not any(threads.for_key("remove", sha256_bytes(corrupt).hex))

    # 4. a manifest that does not hash to its name, evicted when read
    root = tmp_path / "p"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    built = build_tree(root, chunk_size=CS)
    # Test setup, in a thread like every controller path (the loop stays clean).
    await asyncio.to_thread(
        runtime.blobs.cas.put_bytes, built.manifest, built.tree.tree_id
    )
    tree_hex = built.tree.tree_id.hex
    manifest_path = runtime.blobs.path_for(built.tree.tree_id)
    manifest_path.chmod(0o600)
    data = bytearray(built.manifest)
    data[-2] ^= 0x01
    manifest_path.write_bytes(bytes(data))
    async with api_client(runtime, token=token_of(runtime)) as client:
        submit = await client.post(
            "/v1/jobs",
            json={
                "manifest": {"name": "x", "entrypoint": ["python", "a"]},
                "tree_id": str(built.tree.tree_id),
                "placement": {"node_id": str(new_node_id())},
            },
        )
        assert submit.status_code == 409, submit.text
    assert threads.for_key("remove", tree_hex) == [False]

    # 5. GC evictions (quota 1 byte: everything unprotected goes)
    before = len(threads.calls)
    report = await runtime.gc.run_pass("test")
    assert report.evicted >= 2
    evictions = [on for name, _k, on in threads.calls[before:] if name == "remove"]
    assert evictions and not any(evictions)
    assert not any(on for _n, _k, on in threads.calls), threads.calls
