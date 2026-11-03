"""V1.0 G8 F6 tester: cancelling an mDNS browse/close at any moment leaks no sockets."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os

import pytest

from meshforge.agent.discovery import MdnsResolver, browse_controllers
from meshforge.agent.settings import DiscoverySettings

pytestmark = pytest.mark.component


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _settings() -> DiscoverySettings:
    return DiscoverySettings(timeout_s=0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0.0, 0.01, 0.05, 0.1, 0.2, 0.45, 0.5, 0.52, 0.55])
async def test_cancel_resolve_anywhere_leaks_nothing(delay: float) -> None:
    pytest.importorskip("zeroconf")
    gc.collect()
    before = _fds()
    r = MdnsResolver(_settings(), node_id="node_x")
    t = asyncio.create_task(r.resolve())
    await asyncio.sleep(delay)
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t
    gc.collect()
    assert _fds() <= before


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0.0, 0.05, 0.3, 0.55])
async def test_cancel_browse_controllers_leaks_nothing(delay: float) -> None:
    pytest.importorskip("zeroconf")
    gc.collect()
    before = _fds()
    t = asyncio.create_task(browse_controllers(_settings(), timeout_s=0.5))
    await asyncio.sleep(delay)
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t
    gc.collect()
    assert _fds() <= before


@pytest.mark.asyncio
async def test_hanging_close_is_bounded_and_cancel_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("zeroconf")
    import time

    from zeroconf.asyncio import AsyncZeroconf

    from meshforge.agent import discovery

    real_close = AsyncZeroconf.async_close
    closed = []

    async def hang(self: AsyncZeroconf) -> None:
        closed.append(self)
        await asyncio.sleep(3600)

    monkeypatch.setattr(AsyncZeroconf, "async_close", hang)
    monkeypatch.setattr(discovery, "_CLOSE_TIMEOUT_S", 0.3)
    r = MdnsResolver(_settings(), node_id="node_x")
    t = asyncio.create_task(r.resolve())
    await asyncio.sleep(0.1)
    t.cancel()
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t, 3)
    assert 0.2 <= time.monotonic() - t0 < 2.0
    assert t.cancelled()
    for azc in closed:  # really close what the hung stub left open
        monkeypatch.setattr(AsyncZeroconf, "async_close", real_close)
        await azc.async_close()


@pytest.mark.asyncio
async def test_hanging_close_without_cancel_returns_after_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("zeroconf")
    import time

    from zeroconf.asyncio import AsyncZeroconf

    from meshforge.agent import discovery

    real_close = AsyncZeroconf.async_close
    closed = []

    async def hang(self: AsyncZeroconf) -> None:
        closed.append(self)
        await asyncio.sleep(3600)

    monkeypatch.setattr(AsyncZeroconf, "async_close", hang)
    monkeypatch.setattr(discovery, "_CLOSE_TIMEOUT_S", 0.3)
    t0 = time.monotonic()
    try:
        # a live controller on this LAN may answer; only the bound matters
        await asyncio.wait_for(MdnsResolver(_settings(), node_id="n").resolve(), 5)
        assert time.monotonic() - t0 < 1.5
    finally:
        monkeypatch.setattr(AsyncZeroconf, "async_close", real_close)
        for azc in closed:
            await azc.async_close()
