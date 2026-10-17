"""V0.9 G1 tester round 1 (D224/T-14): cancel ordering of the live redaction
worker, hammered from several angles."""

from __future__ import annotations

import asyncio
import threading
import time
from enum import StrEnum

import pytest

from meshforge.agent.execution.live_redaction import LiveRedaction
from meshforge.common.secret_redaction import SecretMatcher, StreamRedactor

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]


class S(StrEnum):
    OUT = "o"
    ERR = "e"


class _Slow(StreamRedactor):
    log: list[str] = []  # noqa: RUF012
    delay = 0.25

    def feed(self, data: bytes) -> bytes:
        _Slow.log.append("start")
        time.sleep(_Slow.delay)
        _Slow.log.append("end")
        return super().feed(data)


def _live() -> LiveRedaction[S]:
    _Slow.log = []
    return LiveRedaction(SecretMatcher({"K": b"hunter2-secret"}), tuple(S), redactor=_Slow)


async def test_a_cancel_waits_for_the_chunk_in_the_thread_even_when_repeated() -> None:
    live = _live()
    task = asyncio.create_task(live.feed(S.OUT, b"x" * 100))
    await asyncio.sleep(0.05)  # the chunk is in the thread
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()  # a second cancel (stop + timeout racing)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _Slow.log == ["start", "end"], "cancel returned while the thread was running"
    live.close()


async def test_nothing_runs_in_the_thread_after_close_following_a_cancel() -> None:
    baseline = {t.ident for t in threading.enumerate()}
    live = _live()
    task = asyncio.create_task(live.feed(S.ERR, b"y" * 10))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    live.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        extra = [
            t for t in threading.enumerate() if t.ident not in baseline and t.is_alive()
        ]
        if not extra:
            break
        await asyncio.sleep(0.05)
    assert not extra, extra
    with pytest.raises(RuntimeError):
        await live.feed(S.OUT, b"late")  # a late feed is refused, not queued


async def test_two_streams_fed_concurrently_never_overlap_in_the_worker() -> None:
    live = _live()
    _Slow.delay = 0.05
    try:
        await asyncio.gather(
            *(live.feed(S.OUT if i % 2 else S.ERR, b"z" * 10) for i in range(6))
        )
    finally:
        _Slow.delay = 0.25
        live.close()
    # strictly alternating start/end: the matcher is never used by two threads
    assert _Slow.log == ["start", "end"] * 6


async def test_a_flush_after_a_cancelled_feed_still_returns_the_held_tail() -> None:
    live: LiveRedaction[S] = LiveRedaction(
        SecretMatcher({"K": b"hunter2-secret"}), tuple(S)
    )
    first = await live.feed(S.OUT, b"abc hunter2-sec")
    task = asyncio.create_task(live.feed(S.OUT, b"ret trailing"))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    tail = await live.flush(S.OUT)
    live.close()
    assert b"hunter2-secret" not in first + tail
