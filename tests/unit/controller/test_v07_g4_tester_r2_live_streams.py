"""V0.7 G4 tester r2, T-1: races and leaks in :class:`LiveStreams` (D157)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.auth.live_streams import (
    MAX_UNVERIFIED_CHECKS,
    LiveStreams,
    StreamEndReason,
)
from meshforge.controller.auth.principal import LEGACY_PRINCIPAL, Principal
from meshforge.controller.store.models.auth import UserRole

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _user(tok: str) -> Principal:
    return Principal(user_id="usr_a", name="a", role=UserRole.USER, token_id=tok)


class _Gate:
    """A checker whose answer is held until the test releases it."""

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def live_token_ids(self, token_ids: Collection[str]) -> set[str]:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return self.live & set(token_ids)


async def test_a_revoke_during_a_running_check_is_not_lost() -> None:
    """recheck_now() while a query is in flight must cause another check."""
    gate = _Gate()
    gate.live = {"t1"}
    streams = LiveStreams(gate, interval_s=30.0)  # the tick can't save us
    h = streams.open(_user("t1"))
    task = asyncio.create_task(streams.run())
    try:
        streams.recheck_now()
        await asyncio.wait_for(gate.entered.wait(), 2)
        gate.live = set()  # revoked after the query's snapshot was taken...
        streams.recheck_now()  # ...and the revoke path wakes the loop again
        gate.release.set()
        await asyncio.wait_for(h.revoked.wait(), 3)
        assert h.reason == StreamEndReason.UNAUTHORIZED
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_stream_opened_and_closed_during_a_check_leaves_no_leak() -> None:
    gate = _Gate()
    streams = LiveStreams(gate, interval_s=30.0)
    h = streams.open(_user("t1"))
    check = asyncio.create_task(streams.check_once())
    await asyncio.wait_for(gate.entered.wait(), 2)
    streams.close(h)  # the client left while the query ran
    streams.close(h)  # idempotent
    gate.release.set()
    assert await check == 0  # a closed stream is not counted as cut
    assert len(streams) == 0 and streams.stats.streams_cut == 0


async def test_legacy_streams_survive_an_outage_that_cuts_user_streams() -> None:
    class Down:
        async def live_token_ids(self, token_ids: Collection[str]) -> set[str]:
            raise ConnectionError("db down")

    streams = LiveStreams(Down(), interval_s=30.0)
    legacy = streams.open(LEGACY_PRINCIPAL)
    user = streams.open(_user("t1"))
    for _ in range(MAX_UNVERIFIED_CHECKS * 2):
        await streams.check_once()
    assert user.reason == StreamEndReason.AUTH_UNAVAILABLE
    assert not legacy.revoked.is_set()  # documented: no token row to re-check
    assert len(streams) == 2  # both still registered until their handlers close


async def test_one_success_resets_the_failure_streak_exactly() -> None:
    class Flaky:
        def __init__(self) -> None:
            self.fail = True

        async def live_token_ids(self, token_ids: Collection[str]) -> set[str]:
            if self.fail:
                raise TimeoutError
            return set(token_ids)

    flaky = Flaky()
    streams = LiveStreams(flaky, interval_s=30.0)
    h = streams.open(_user("t1"))
    for _ in range(3):  # many cycles of (N-1 failures, 1 success) never cut
        flaky.fail = True
        for _ in range(MAX_UNVERIFIED_CHECKS - 1):
            await streams.check_once()
        flaky.fail = False
        await streams.check_once()
    assert not h.revoked.is_set()
    flaky.fail = True
    for _ in range(MAX_UNVERIFIED_CHECKS):
        await streams.check_once()
    assert h.reason == StreamEndReason.AUTH_UNAVAILABLE


async def test_run_survives_a_checker_that_raises_every_time() -> None:
    class Boom:
        calls = 0

        async def live_token_ids(self, token_ids: Collection[str]) -> set[str]:
            Boom.calls += 1
            raise RuntimeError("boom")

    streams = LiveStreams(Boom(), interval_s=0.01)
    streams.open(_user("t1"))
    task = asyncio.create_task(streams.run())
    await asyncio.sleep(0.3)
    assert not task.done()  # the loop must never die: a dead loop = authed forever
    assert Boom.calls > MAX_UNVERIFIED_CHECKS
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_ten_thousand_streams_one_query_and_full_cleanup() -> None:
    class All:
        calls = 0

        async def live_token_ids(self, token_ids: Collection[str]) -> set[str]:
            All.calls += 1
            return set()

    streams = LiveStreams(All(), interval_s=30.0)
    hs = [streams.open(_user(f"t{i % 50}")) for i in range(10_000)]
    assert await streams.check_once() == 10_000
    assert All.calls == 1
    for h in hs:
        streams.close(h)
    assert len(streams) == 0
