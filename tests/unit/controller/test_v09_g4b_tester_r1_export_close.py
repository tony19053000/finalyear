"""V0.9 G4 lane B tester r1: more hang-up shapes for the export stream."""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest
import structlog

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.controller.api import telemetry_routes as routes

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _gen(state: dict[str, bool], count: int = 50) -> Generator[bytes, None, None]:
    try:
        for i in range(count):
            yield bytes([i % 250]) * 1000
    finally:
        state["closed"] = True


async def test_hang_up_before_the_first_chunk_closes_the_generator() -> None:
    state = {"closed": False}
    stream = routes._stream(_gen(state), rows=1, campaign=None, user_id="u")
    await stream.aclose()  # never started: the generator was never entered
    # an un-entered generator closes without running its finally; it must not leak
    # an exception and a second close is harmless
    await stream.aclose()


async def test_hang_up_after_every_possible_chunk_never_aborts() -> None:
    for after in (1, 2, 10, 49):
        state = {"closed": False}
        stream = routes._stream(_gen(state), rows=50, campaign="c", user_id="u")
        with structlog.testing.capture_logs() as logs:
            for _ in range(after):
                await anext(stream)
            await stream.aclose()
        assert state["closed"], after
        assert not [e for e in logs if e["event"].endswith("export_aborted")], after


async def test_task_cancel_between_chunks_closes_the_generator() -> None:
    state = {"closed": False}
    stream = routes._stream(_gen(state), rows=50, campaign=None, user_id="u")

    async def consume() -> None:
        async for _ in stream:
            await asyncio.sleep(0.01)

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()
    assert state["closed"]


async def test_an_empty_export_completes() -> None:
    state = {"closed": False}
    got = [
        c
        async for c in routes._stream(
            _gen(state, 0), rows=0, campaign=None, user_id="u"
        )
    ]
    assert got == [] and state["closed"]
