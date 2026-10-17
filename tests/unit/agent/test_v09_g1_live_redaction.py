"""V0.9 G1 item 6 (D224, THREAT_MODEL RE-D2): the agent's live log redaction
runs in one worker thread per attempt.

* a slow redaction (RE-D2's measured worst case) does not stall the loop;
* the output is byte-for-byte the on-loop redaction's, at any chunking, and
  the two streams keep their own carry-over;
* T-14 regression: a cancel while a chunk is in the thread waits for that
  chunk before it re-raises, so the attempt's cleanup (secrets wiped, matcher
  dropped) never runs while redaction still uses them; the drain (flush)
  always runs after the last feed.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from enum import StrEnum
from typing import ClassVar

import pytest

from meshforge.agent.execution.live_redaction import LiveRedaction
from meshforge.common.secret_redaction import (
    SecretMatcher,
    StreamRedactor,
    redact_bytes,
)

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

SLOW_SECRETS = {f"S{i}": b"a" * (8192 - i) for i in range(32)}
CHUNK = 64 * 1024


class S(StrEnum):
    OUT = "stdout"
    ERR = "stderr"


async def test_a_slow_redaction_does_not_block_the_event_loop() -> None:
    matcher = await asyncio.to_thread(SecretMatcher, SLOW_SECRETS)
    chunk = b"a" * CHUNK
    started = time.perf_counter()
    redact_bytes(chunk, matcher)
    cost_s = time.perf_counter() - started
    if cost_s < 0.06:
        pytest.skip(f"redaction too fast here to measure a stall ({cost_s:.3f} s)")
    matcher = await asyncio.to_thread(SecretMatcher, SLOW_SECRETS)
    live = LiveRedaction(matcher, tuple(S))
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:  # heartbeats/pongs want the loop every 2 ms
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        out = b""
        for _ in range(3):
            out += await live.feed(S.OUT, chunk)
        out += await live.flush(S.OUT)
    finally:
        stop.set()
        await tick
        live.close()
    assert out == redact_bytes(chunk * 3, SecretMatcher(SLOW_SECRETS))
    worst = max(gaps)
    assert worst < min(0.1, cost_s / 2), (worst, cost_s)


async def test_output_equals_the_on_loop_redaction_at_any_chunking() -> None:
    rng = random.Random(7)  # noqa: S311 - a reproducible chunking, not crypto
    secrets = {"K": b"hunter2-secret", "T": b"tok_abcdefgh"}
    text = b"".join(
        rng.choice([b"x" * rng.randint(1, 40), b"hunter2-secret", b"tok_abcdefgh"])
        for _ in range(400)
    )
    expected = redact_bytes(text, SecretMatcher(secrets))
    for _ in range(20):
        live = LiveRedaction(SecretMatcher(secrets), tuple(S))
        out = {S.OUT: b"", S.ERR: b""}
        pos = 0
        while pos < len(text):
            step = rng.randint(1, 64)
            for stream in S:  # interleaved streams, same data, own carry-over
                out[stream] += await live.feed(stream, text[pos : pos + step])
            pos += step
        for stream in S:
            out[stream] += await live.flush(stream)
        live.close()
        assert out[S.OUT] == expected and out[S.ERR] == expected


class _SlowRedactor(StreamRedactor):
    """Blocks in ``feed`` until released; records whether it is running."""

    running = threading.Event()
    release = threading.Event()
    finished: ClassVar[list[float]] = []

    def feed(self, data: bytes) -> bytes:
        type(self).running.set()
        type(self).release.wait(5)
        result = super().feed(data)
        type(self).finished.append(time.monotonic())
        return result


async def test_t14_a_cancel_waits_for_the_chunk_in_the_thread() -> None:
    _SlowRedactor.running.clear()
    _SlowRedactor.release.clear()
    _SlowRedactor.finished.clear()
    live = LiveRedaction(
        SecretMatcher({"K": b"secret-value"}), tuple(S), redactor=_SlowRedactor
    )
    pump = asyncio.create_task(live.feed(S.OUT, b"leak secret-value here"))
    await asyncio.to_thread(_SlowRedactor.running.wait, 5)
    pump.cancel()  # the attempt is stopped while the chunk is in the thread
    await asyncio.sleep(0.05)
    assert not pump.done(), "the cancel must wait for the thread"
    _SlowRedactor.release.set()
    with pytest.raises(asyncio.CancelledError):
        await pump
    cancelled_at = time.monotonic()
    # The chunk finished BEFORE the cancel reached the caller: its cleanup
    # (wipe secrets, ACTIVE.remove) can no longer race the redaction.
    assert _SlowRedactor.finished and _SlowRedactor.finished[0] <= cancelled_at
    live.close()
    with pytest.raises(RuntimeError):
        await live.feed(S.OUT, b"after close")


async def test_the_drain_comes_after_every_feed() -> None:
    order: list[str] = []

    class Recording(StreamRedactor):
        def feed(self, data: bytes) -> bytes:
            time.sleep(0.01)
            order.append("feed")
            return super().feed(data)

        def flush(self) -> bytes:
            order.append("flush")
            return super().flush()

    live = LiveRedaction(
        SecretMatcher({"K": b"secret-value"}), tuple(S), redactor=Recording
    )
    feeds = [asyncio.create_task(live.feed(S.OUT, b"chunk ")) for _ in range(5)]
    await asyncio.gather(*feeds)
    await live.flush(S.OUT)
    live.close()
    assert order == ["feed"] * 5 + ["flush"]
