# ruff: noqa: S311, BLE001, S110, SIM105
"""V0.5 G1 round 2 tester: the reference-counted per-digest locks of BlobStore."""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.controller.blobs import BlobStore

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _store(tmp_path: Path) -> BlobStore:
    store = BlobStore(tmp_path / "blobs", max_blob_bytes=64 * 1024 * 1024)
    store.prepare()
    return store


def _locks(store: BlobStore) -> dict[str, object]:
    return store._part_locks


async def test_no_lock_leak_after_1000_mixed_operations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blobs = [os.urandom(2 * CS + i) for i in range(25)]
    digests = [sha256_bytes(b) for b in blobs]

    async def part(i: int, k: int) -> None:
        data = blobs[i]
        piece = data[k * CS : (k + 1) * CS]
        try:
            await store.put_part(
                digests[i],
                k,
                size=len(data),
                chunk_size=CS,
                chunk_hex=hashlib.sha256(piece).hexdigest(),
                body=_one(piece),
            )
        except Exception:
            pass

    async def status(i: int) -> None:
        await store.part_status(digests[i])

    async def complete(i: int) -> None:
        try:
            await store.complete_parts(digests[i])
        except Exception:
            pass

    rng = random.Random(7)
    tasks = []
    for _ in range(1000):
        i = rng.randrange(len(blobs))
        kind = rng.choice(["part", "part", "part", "status", "complete"])
        if kind == "part":
            tasks.append(part(i, rng.randrange(3)))
        elif kind == "status":
            tasks.append(status(i))
        else:
            tasks.append(complete(i))
    await asyncio.gather(*tasks)
    assert _locks(store) == {}, "lock entries leaked"
    # Finish honestly: every blob must end up exact.
    for i, data in enumerate(blobs):
        for k in range(3):
            await part(i, k)
        await complete(i)
        assert store.size(digests[i]) == len(data)
        assert store.path_for(digests[i]).read_bytes() == data
    assert _locks(store) == {}


async def test_waiter_cancelled_mid_wait_releases_its_reference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = Digest("a" * 64)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def holder() -> None:
        async with store._holding(d):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()

    async def waiter() -> None:
        async with store._holding(d):
            pass

    waiters = [asyncio.create_task(waiter()) for _ in range(20)]
    await asyncio.sleep(0.05)
    assert _locks(store)[d.hex][1] == 21  # type: ignore[index]
    for w in waiters[::2]:
        w.cancel()
    await asyncio.sleep(0.05)
    assert _locks(store)[d.hex][1] == 11  # type: ignore[index]
    # A newcomer during the wait must queue on the same lock, not a fresh one.
    order: list[str] = []

    async def newcomer() -> None:
        async with store._holding(d):
            order.append("newcomer")

    n = asyncio.create_task(newcomer())
    await asyncio.sleep(0.02)
    assert order == []  # still excluded by the holder
    release.set()
    await asyncio.gather(h, n, *waiters, return_exceptions=True)
    assert order == ["newcomer"]
    assert _locks(store) == {}


async def test_holder_cancelled_and_raising_do_not_leak_or_deadlock(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    d = Digest("b" * 64)

    async def boom() -> None:
        async with store._holding(d):
            raise RuntimeError("x")

    for _ in range(50):
        with pytest.raises(RuntimeError):
            await boom()
    assert _locks(store) == {}

    async def sleeper() -> None:
        async with store._holding(d):
            await asyncio.sleep(30)

    tasks = [asyncio.create_task(sleeper()) for _ in range(10)]
    await asyncio.sleep(0.02)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert _locks(store) == {}
    async with asyncio.timeout(2):
        async with store._holding(d):
            pass


async def test_mutual_exclusion_holds_with_churn_across_many_digests(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    digests = [Digest(f"{i:064x}") for i in range(40)]
    inside: dict[str, int] = {}
    violations = 0

    async def work(d: Digest) -> None:
        nonlocal violations
        async with store._holding(d):
            inside[d.hex] = inside.get(d.hex, 0) + 1
            if inside[d.hex] != 1:
                violations += 1
            await asyncio.sleep(0)
            inside[d.hex] -= 1

    rng = random.Random(3)
    await asyncio.gather(*[work(rng.choice(digests)) for _ in range(3000)])
    assert violations == 0
    assert _locks(store) == {}
