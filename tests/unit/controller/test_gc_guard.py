"""V0.5 G5: the controller's GC guard - submissions share it, a GC pass owns it."""

from __future__ import annotations

import asyncio

import pytest

from meshforge.controller.blobs import GcGuard

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_shared_holders_run_together_and_exclusive_waits_for_them() -> None:
    guard = GcGuard()
    order: list[str] = []
    release = asyncio.Event()

    async def reader(name: str) -> None:
        async with guard.shared():
            order.append(f"{name}+")
            await release.wait()
            order.append(f"{name}-")

    async def writer() -> None:
        async with guard.exclusive():
            order.append("gc")

    readers = [asyncio.create_task(reader(n)) for n in ("a", "b")]
    await asyncio.sleep(0)
    gc = asyncio.create_task(writer())
    await asyncio.sleep(0.01)
    assert order == ["a+", "b+"]  # both readers in at once, the pass waits
    late = asyncio.create_task(reader("c"))
    await asyncio.sleep(0.01)
    assert "c+" not in order  # a waiting pass blocks new submissions
    release.set()
    await asyncio.gather(*readers, gc, late)
    assert order.index("gc") > order.index("b-")
    assert order.index("c+") > order.index("gc")


async def test_a_cancelled_exclusive_waiter_does_not_block_readers() -> None:
    guard = GcGuard()
    hold = asyncio.Event()

    async def reader() -> None:
        async with guard.shared():
            await hold.wait()

    first = asyncio.create_task(reader())
    await asyncio.sleep(0)

    async def writer() -> None:
        async with guard.exclusive():
            pass

    waiting = asyncio.create_task(writer())
    await asyncio.sleep(0.01)
    entered = asyncio.Event()

    async def queued_reader() -> None:
        async with guard.shared():
            entered.set()

    held_back = asyncio.create_task(queued_reader())  # behind the waiting pass
    await asyncio.sleep(0.01)
    assert not entered.is_set()
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    async with asyncio.timeout(1):
        await entered.wait()  # woken by the cancelled waiter, not by the reader
    await held_back
    hold.set()
    await first
