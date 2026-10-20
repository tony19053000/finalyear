"""V0.10 G6 tester r1: an early CancelChunk is exact - it names one (item, lease
seq) and skips only that grant, once; a duplicate delivery does not poison a
re-grant of the same chunk under a later lease seq."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_v10_g6_lane_cancel import TOKEN, _attempt, _cancel, _grant, _Host

pytestmark = pytest.mark.unit


def test_early_cancel_skips_only_the_named_lease_seq(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    host._lane_command(_cancel(41, 2))
    # the same chunk granted again under seq 3 is NOT skipped: it proceeds into
    # the staging path (which this stub host cannot run) instead of returning
    with pytest.raises(Exception):  # noqa: B017 - any failure past the skip gate
        asyncio.run(
            host._chunk(attempt, None, None, None, None, None, _grant(41, 3))  # type: ignore[arg-type]
        )
    assert (41, 2) in attempt.lane.cancelled_early


def test_duplicate_cancel_is_consumed_once(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    host._lane_command(_cancel(7, 1))
    host._lane_command(_cancel(7, 1))
    assert len(attempt.lane.cancelled_early) == 1
    asyncio.run(host._chunk(attempt, None, None, None, None, None, _grant(7, 1)))  # type: ignore[arg-type]
    assert not attempt.lane.cancelled_early


def test_cancel_for_another_attempt_is_ignored(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    host = _Host(attempt)
    msg: Any = _cancel(8, 1)
    msg.cancel_chunk.attempt_id = "someone-else"
    host._lane_command(msg)
    assert not attempt.lane.cancelled_early
    assert attempt.token == TOKEN
