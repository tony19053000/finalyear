"""V0.4 G5 D71: live log batching (64 KiB / 100 ms, injected clock) and the cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.execution.logs import (
    FLUSH_BYTES,
    FLUSH_S,
    AttemptLogs,
    LogBatcher,
    truncation_marker,
)
from meshforge.agent.execution.runtime import Stream

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def test_a_batch_flushes_after_100_ms_and_not_before() -> None:
    clock = Clock()
    batcher = LogBatcher(clock=clock)
    assert batcher.add(Stream.STDOUT, b"a") == []
    assert batcher.next_deadline() == pytest.approx(50.0 + FLUSH_S)
    clock.now = 50.0 + FLUSH_S - 0.001
    batcher.add(Stream.STDOUT, b"b")  # does not restart the wait
    assert batcher.due() == []
    clock.now = 50.0 + FLUSH_S
    (chunk,) = batcher.due()
    assert (chunk.stream, chunk.offset, chunk.data) == (Stream.STDOUT, 0, b"ab")
    assert batcher.next_deadline() is None and batcher.due() == []


def test_64_kib_flushes_at_once_with_contiguous_offsets() -> None:
    batcher = LogBatcher(clock=Clock())
    assert batcher.add(Stream.STDERR, b"x" * (FLUSH_BYTES - 1)) == []
    (full,) = batcher.add(Stream.STDERR, b"yz")  # max+1: one full chunk out
    assert full.offset == 0 and len(full.data) == FLUSH_BYTES
    (rest,) = batcher.flush()
    assert rest.offset == FLUSH_BYTES and rest.data == b"z"
    big = batcher.add(Stream.STDERR, b"q" * (2 * FLUSH_BYTES + 5))
    assert [c.offset for c in big] == [FLUSH_BYTES + 1, 2 * FLUSH_BYTES + 1]


def test_streams_are_batched_separately() -> None:
    clock = Clock()
    batcher = LogBatcher(clock=clock)
    batcher.add(Stream.STDOUT, b"o")
    clock.now = 50.0 + FLUSH_S / 2
    batcher.add(Stream.STDERR, b"e")
    clock.now = 50.0 + FLUSH_S
    assert [c.stream for c in batcher.due()] == [Stream.STDOUT]
    clock.now = 50.0 + FLUSH_S * 1.5
    (err,) = batcher.due()
    assert err.stream is Stream.STDERR and err.offset == 0


def test_cap_max_kept_then_truncated_with_a_marker(tmp_path: Path) -> None:
    logs = AttemptLogs(tmp_path / "logs", max_bytes=10, clock=Clock())
    logs.write(Stream.STDOUT, b"0123456789")  # exactly the cap: kept
    assert not logs.truncated[Stream.STDOUT]
    logs.write(Stream.STDOUT, b"X")  # cap + 1
    assert logs.truncated[Stream.STDOUT] and logs.kept[Stream.STDOUT] == 10
    logs.write(Stream.STDOUT, b"more")  # nothing after the marker
    live = b"".join(c.data for c in logs.batcher.flush())
    logs.close()
    on_disk = logs.path(Stream.STDOUT).read_bytes()
    assert on_disk == b"0123456789" + truncation_marker(10)
    assert live == on_disk  # the live stream says so too
    assert logs.path(Stream.STDERR).read_bytes() == b""


def test_a_write_that_crosses_the_cap_keeps_the_part_below_it(tmp_path: Path) -> None:
    logs = AttemptLogs(tmp_path / "logs", max_bytes=4, clock=Clock())
    logs.write(Stream.STDERR, b"abcdef")
    logs.close()
    assert logs.path(Stream.STDERR).read_bytes() == b"abcd" + truncation_marker(4)
