"""V1.0 G8 F6 - cleanup that a cancellation must not cut short.

CI shard 6 errored at setup of an unrelated test with 8 ``ResourceWarning:
unclosed transport/socket ... 5353``: an agent stopped while its mDNS browse
was closing left the zeroconf sockets open. ``run_to_completion`` finishes the
close and re-raises the cancel; the mDNS close paths use it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from meshforge.common.aio import run_to_completion

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _SlowClose:
    """A resource whose close takes several loop turns (like zeroconf's)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False
        self.unregistered = False

    async def async_close(self) -> None:
        self.started.set()
        for _ in range(5):
            await asyncio.sleep(0.01)
        self.closed = True

    async def async_cancel(self) -> None:  # the browser half
        self.started.set()
        await asyncio.sleep(0.01)

    async def async_unregister_service(self, _info: Any) -> None:
        self.started.set()
        await asyncio.sleep(0.01)
        self.unregistered = True


async def test_completes_the_work_and_re_raises_the_cancel() -> None:
    res = _SlowClose()
    task = asyncio.ensure_future(run_to_completion(res.async_close()))
    await res.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert res.closed


async def test_survives_repeated_cancels() -> None:
    res = _SlowClose()
    task = asyncio.ensure_future(run_to_completion(res.async_close()))
    await res.started.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0.005)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert res.closed


async def test_returns_the_result_without_a_cancel() -> None:
    async def work() -> int:
        await asyncio.sleep(0)
        return 7

    assert await run_to_completion(work()) == 7


async def test_the_work_error_propagates() -> None:
    async def work() -> None:
        await asyncio.sleep(0)
        raise OSError("boom")

    with pytest.raises(OSError, match="boom"):
        await run_to_completion(work())


async def test_the_work_error_wins_over_the_cancel() -> None:
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        await asyncio.sleep(0.02)
        raise OSError("boom")

    task = asyncio.ensure_future(run_to_completion(work()))
    await started.wait()
    task.cancel()
    with pytest.raises(OSError, match="boom"):
        await task


async def test_agent_browse_close_finishes_under_cancel() -> None:
    from meshforge.agent.discovery import _close_browse

    azc, browser = _SlowClose(), _SlowClose()
    task = asyncio.ensure_future(_close_browse(azc, browser))
    await browser.started.wait()  # cancelled while the browser is cancelled
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert azc.closed


async def test_controller_advertiser_stop_finishes_under_cancel() -> None:
    from meshforge.controller.discovery import ServiceAdvertiser

    adv = ServiceAdvertiser(service_type="_mf-x._tcp.local.", host="127.0.0.1", port=1)
    azc = _SlowClose()
    adv._azc, adv._info = azc, object()
    task = asyncio.ensure_future(adv.stop())
    await azc.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert azc.unregistered and azc.closed


# --------------------------------------------- controller start's cleanup close


class _FailingRegistration(_SlowClose):
    def __init__(self, *_a: Any, **_k: Any) -> None:
        super().__init__()
        self.registering = asyncio.Event()

    async def async_register_service(self, _info: Any) -> None:
        self.registering.set()
        await asyncio.sleep(0.01)
        raise OSError("no multicast interface")


def _fake_zeroconf(monkeypatch: pytest.MonkeyPatch, azc: _SlowClose) -> None:
    import zeroconf.asyncio as zc_asyncio

    monkeypatch.setattr(zc_asyncio, "AsyncZeroconf", lambda: azc)
    monkeypatch.setattr(zc_asyncio, "AsyncServiceInfo", lambda *a, **k: object())


def _advertiser() -> Any:
    from meshforge.controller.discovery import ServiceAdvertiser

    return ServiceAdvertiser(service_type="_mf-x._tcp.local.", host="127.0.0.1", port=1)


async def test_cancel_during_controller_start_cleanup_close_is_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed registration closes the instance; a cancel landing in that
    close still finishes it and surfaces as CancelledError (not swallowed by
    the best-effort ``suppress(Exception)``)."""
    pytest.importorskip("zeroconf")
    azc = _FailingRegistration()
    _fake_zeroconf(monkeypatch, azc)
    adv = _advertiser()
    task = asyncio.ensure_future(adv.start())
    await azc.registering.wait()
    azc.started.clear()
    await azc.started.wait()  # now inside async_close
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert azc.closed and not adv.registered


async def test_cancel_during_registration_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("zeroconf")
    azc = _FailingRegistration()
    _fake_zeroconf(monkeypatch, azc)
    adv = _advertiser()
    task = asyncio.ensure_future(adv.start())
    await azc.registering.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert azc.closed and not adv.registered


# ------------------------------------------------------ bounded (5 s) closes


class _WedgedClose:
    async def async_close(self) -> None:
        await asyncio.sleep(3600)

    async def async_unregister_service(self, _info: Any) -> None:
        return None


async def test_wedged_agent_browse_close_is_bounded_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structlog

    from meshforge.agent import discovery

    monkeypatch.setattr(discovery, "_CLOSE_TIMEOUT_S", 0.05)
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(discovery._close_browse(_WedgedClose(), None), 5)
    assert [e["event"] for e in logs] == ["agent.discovery.close_timeout"]


async def test_wedged_controller_withdraw_is_bounded_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import structlog

    from meshforge.controller import discovery

    monkeypatch.setattr(discovery, "_CLOSE_TIMEOUT_S", 0.05)
    adv = _advertiser()
    adv._azc, adv._info = _WedgedClose(), object()
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(adv.stop(), 5)
    events = [e["event"] for e in logs]
    assert events[0] == "controller.discovery.close_timeout", events


async def test_finished_within() -> None:
    from meshforge.common.aio import finished_within

    assert await finished_within(asyncio.sleep(0), 1.0)
    assert not await finished_within(asyncio.sleep(3600), 0.01)
