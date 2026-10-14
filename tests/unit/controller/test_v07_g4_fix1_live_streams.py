"""D157 T-1: :class:`LiveStreams` - open streams re-check their API token."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.controller.auth.live_streams import (
    MAX_UNVERIFIED_CHECKS,
    LiveStreams,
    StreamEndReason,
)
from meshforge.controller.auth.principal import LEGACY_PRINCIPAL, Principal
from meshforge.controller.store.models.auth import UserRole

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.security]


def _user(token_id: str, user_id: str = "usr_a") -> Principal:
    return Principal(user_id=user_id, name="a", role=UserRole.USER, token_id=token_id)


@dataclass
class _Checker:
    live: set[str] = field(default_factory=set)
    calls: list[frozenset[str]] = field(default_factory=list)
    error: BaseException | None = None

    async def live_token_ids(self, token_ids: Collection[str]) -> set[str]:
        self.calls.append(frozenset(token_ids))
        if self.error is not None:
            raise self.error
        return self.live & set(token_ids)


async def test_a_token_that_is_no_longer_live_ends_only_its_streams() -> None:
    checker = _Checker(live={"tok_1", "tok_2"})
    streams = LiveStreams(checker, interval_s=1.0)
    a1, a2, b = (
        streams.open(_user("tok_1")),
        streams.open(_user("tok_1")),
        streams.open(_user("tok_2", "usr_b")),
    )
    assert await streams.check_once() == 0
    assert checker.calls == [frozenset({"tok_1", "tok_2"})]  # one query for all
    checker.live.discard("tok_1")
    assert await streams.check_once() == 2
    assert a1.revoked.is_set() and a2.revoked.is_set() and not b.revoked.is_set()
    assert a1.reason == StreamEndReason.UNAUTHORIZED
    assert await streams.check_once() == 0  # ended once, not again
    assert streams.stats.streams_cut == 2


async def test_closed_streams_are_not_checked_and_nothing_open_means_no_query() -> None:
    checker = _Checker(live=set())
    streams = LiveStreams(checker, interval_s=1.0)
    handle = streams.open(_user("tok_1"))
    streams.close(handle)
    streams.close(handle)  # idempotent
    assert len(streams) == 0
    assert await streams.check_once() == 0
    assert checker.calls == []
    assert not handle.revoked.is_set()


async def test_the_legacy_token_has_no_row_and_is_not_rechecked() -> None:
    checker = _Checker(live=set())
    streams = LiveStreams(checker, interval_s=1.0)
    handle = streams.open(LEGACY_PRINCIPAL)
    assert await streams.check_once() == 0
    assert checker.calls == [] and not handle.revoked.is_set()


@pytest.mark.parametrize(
    "error",
    [
        MeshForgeError("db down", failure_type=FailureType.NETWORK_ERROR),
        RuntimeError("a bug in the checker"),
    ],
)
async def test_a_database_that_cannot_answer_fails_closed_after_a_few_checks(
    error: BaseException,
) -> None:
    checker = _Checker(live={"tok_1"}, error=error)
    streams = LiveStreams(checker, interval_s=1.0)
    handle = streams.open(_user("tok_1"))
    for _ in range(MAX_UNVERIFIED_CHECKS - 1):
        assert await streams.check_once() == 0
        assert not handle.revoked.is_set()
    assert await streams.check_once() == 1
    assert handle.reason == StreamEndReason.AUTH_UNAVAILABLE
    assert streams.stats.check_failures == MAX_UNVERIFIED_CHECKS


async def test_one_good_check_resets_the_failure_count() -> None:
    checker = _Checker(live={"tok_1"}, error=MeshForgeError("down"))
    streams = LiveStreams(checker, interval_s=1.0)
    handle = streams.open(_user("tok_1"))
    for _ in range(MAX_UNVERIFIED_CHECKS - 1):
        await streams.check_once()
    checker.error = None
    await streams.check_once()
    checker.error = MeshForgeError("down")
    for _ in range(MAX_UNVERIFIED_CHECKS - 1):
        await streams.check_once()
    assert not handle.revoked.is_set()


async def test_the_loop_ticks_and_recheck_now_wakes_it_at_once() -> None:
    checker = _Checker(live={"tok_1"})
    streams = LiveStreams(checker, interval_s=0.05)
    handle = streams.open(_user("tok_1"))
    task = asyncio.create_task(streams.run())
    try:
        await asyncio.sleep(0.2)
        assert len(checker.calls) >= 2  # periodic
        checker.live.clear()
        await asyncio.wait_for(handle.revoked.wait(), 1.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    slow = LiveStreams(_Checker(live=set()), interval_s=60.0)
    other = slow.open(_user("tok_9"))
    task = asyncio.create_task(slow.run())
    try:
        await asyncio.sleep(0.05)
        assert not other.revoked.is_set()  # no tick in 60 s ...
        slow.recheck_now()  # ... but a REST revoke wakes it
        await asyncio.wait_for(other.revoked.wait(), 1.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        LiveStreams(_Checker(), interval_s=0)
