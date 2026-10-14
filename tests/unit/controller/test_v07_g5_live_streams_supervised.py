"""V0.7 G5 item 0, G4 reviewer N4 (D159): ``LiveStreams.run`` is supervised.

An exception escaping a re-check pass (a bug, not a database error - those are
handled inside ``check_once``) is logged, the loop backs off and goes on; after
``MAX_UNVERIFIED_CHECKS`` crashes in a row the streams are closed (fail closed)
and the controller's ``/healthz`` says ``degraded`` until a pass succeeds.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Collection

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from typing import Any, cast

import httpx
import structlog

from meshforge.controller.api.auth import ApiAuth
from meshforge.controller.auth.live_streams import (
    MAX_UNVERIFIED_CHECKS,
    LiveStreams,
    StreamEndReason,
)
from meshforge.controller.auth.principal import LEGACY_PRINCIPAL, Principal
from meshforge.controller.store.models.auth import UserRole

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _user(tok: str) -> Principal:
    return Principal(user_id="usr_a", name="a", role=UserRole.USER, token_id=tok)


class _AllLive:
    async def live_token_ids(self, token_ids: Collection[str]) -> set[str]:
        return set(token_ids)


class _Flaky(LiveStreams):
    """check_once raises (a bug) while ``crash`` is set."""

    crash = True
    passes = 0

    async def check_once(self) -> int:
        self.passes += 1
        if self.crash:
            raise RuntimeError("bug in a pass")
        return await super().check_once()


async def _until(cond: Callable[[], bool], within_s: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + within_s
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "not reached"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_a_crashing_pass_is_logged_and_the_loop_goes_on() -> None:
    streams = _Flaky(_AllLive(), interval_s=0.001)
    handle = streams.open(_user("t1"))
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(streams.run())
        try:
            await _until(lambda: streams.stats.loop_crashes >= 2)
            assert not task.done()
            assert not handle.revoked.is_set()  # 2 crashes: not yet fail-closed
            assert not streams.degraded
            streams.crash = False
            before = streams.passes
            await _until(lambda: streams.passes > before)
            await _until(lambda: streams._crashes == 0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    events = [e for e in logs if e["event"] == "controller.auth.stream_recheck_crashed"]
    assert events and events[0]["cause"] == "RuntimeError"
    assert any(e["event"] == "controller.auth.stream_recheck_recovered" for e in logs)


@pytest.mark.asyncio
async def test_repeated_crashes_close_streams_and_degrade_until_a_pass_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshforge.controller.auth.live_streams as mod

    monkeypatch.setattr(mod, "MAX_CRASH_BACKOFF_S", 0.01)
    streams = _Flaky(_AllLive(), interval_s=0.001)
    user = streams.open(_user("t1"))
    legacy = streams.open(LEGACY_PRINCIPAL)
    task = asyncio.create_task(streams.run())
    try:
        await _until(lambda: streams.stats.loop_crashes >= MAX_UNVERIFIED_CHECKS)
        await _until(lambda: user.revoked.is_set())
        assert user.reason == StreamEndReason.AUTH_UNAVAILABLE
        assert not legacy.revoked.is_set()  # N3: legacy streams are not re-checked
        assert streams.degraded
        assert not task.done()
        streams.crash = False
        await _until(lambda: not streams.degraded)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_the_backoff_grows_and_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    import meshforge.controller.auth.live_streams as mod

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    streams = LiveStreams(_AllLive(), interval_s=1.0)
    for _ in range(8):
        await streams._crashed(RuntimeError("x"))
    assert sleeps[:5] == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert max(sleeps) == mod.MAX_CRASH_BACKOFF_S


class _NoSessions:
    def __len__(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_healthz_reports_degraded_while_the_loop_keeps_crashing() -> None:
    from meshforge.controller.http import create_http_app

    streams = LiveStreams(_AllLive(), interval_s=1.0)
    app = create_http_app(
        cast("Any", _NoSessions()),
        auth=ApiAuth(verifier=None, live_streams=streams),
        registry=cast("Any", object()),
        resources=cast("Any", object()),
        clock=lambda: 0.0,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/healthz")).json() == {"status": "ok", "sessions": 0}
        streams._crashes = MAX_UNVERIFIED_CHECKS
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "degraded",
            "sessions": 0,
            "degraded": ["stream-auth-recheck"],
        }
