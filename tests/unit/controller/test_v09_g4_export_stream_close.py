"""V0.9 G4 (lane A reviewer follow-up): the telemetry export's ``_stream``
closes the Parquet piece generator deterministically when the client hangs up,
and a hang-up is not logged as ``export_aborted``."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator
from typing import Any

import pytest
import structlog

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.controller.api import telemetry_routes as routes

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Pieces:
    """A piece generator that records whether it was closed."""

    def __init__(self, count: int, *, fail_at: int | None = None) -> None:
        self.closed = False
        self.count = count
        self.fail_at = fail_at

    def gen(self) -> Generator[bytes, None, None]:
        try:
            for i in range(self.count):
                if i == self.fail_at:
                    raise RuntimeError("encode failed")
                yield bytes([i]) * 10
        finally:
            self.closed = True


def _events(logs: list[dict[str, Any]]) -> list[str]:
    return [e["event"] for e in logs]


async def test_client_hang_up_closes_the_pieces_and_is_not_an_abort() -> None:
    source = _Pieces(5)
    stream = routes._stream(source.gen(), rows=5, campaign=None, user_id="u")
    with structlog.testing.capture_logs() as logs:
        assert await anext(stream) == b"\x00" * 10
        await stream.aclose()  # what Starlette does on a disconnect
    assert source.closed
    assert "controller.telemetry.export_aborted" not in _events(logs)
    assert "controller.telemetry.exported" not in _events(logs)


async def test_complete_export_is_logged_and_closed() -> None:
    source = _Pieces(3)
    with structlog.testing.capture_logs() as logs:
        got = [
            chunk
            async for chunk in routes._stream(
                source.gen(), rows=3, campaign="camp_1", user_id="u"
            )
        ]
    assert b"".join(got) == b"".join(bytes([i]) * 10 for i in range(3))
    assert source.closed
    (done,) = [e for e in logs if e["event"] == "controller.telemetry.exported"]
    assert done["bytes"] == 30 and done["rows"] == 3


async def test_encode_failure_is_logged_as_abort_and_raised() -> None:
    source = _Pieces(5, fail_at=2)
    with structlog.testing.capture_logs() as logs, pytest.raises(RuntimeError):
        async for _ in routes._stream(source.gen(), rows=5, campaign=None, user_id="u"):
            pass
    assert source.closed
    (aborted,) = [
        e for e in logs if e["event"] == "controller.telemetry.export_aborted"
    ]
    assert aborted["error_type"] == "RuntimeError" and aborted["bytes_sent"] == 20


async def test_cancel_while_a_worker_thread_encodes_does_not_raise_from_close() -> None:
    """Cancelled inside ``await to_thread(next, ...)``: the generator is still
    executing in the worker, ``close()`` raises ValueError there - swallowed,
    only the cancellation propagates."""
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def slow() -> Generator[bytes, None, None]:
        try:
            entered.set()
            release.wait(10)
            yield b"x"
        finally:
            closed.set()

    pieces = slow()
    stream = routes._stream(pieces, rows=1, campaign=None, user_id="u")
    task = asyncio.ensure_future(anext(stream))
    await asyncio.to_thread(entered.wait, 10)
    with structlog.testing.capture_logs() as logs:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "controller.telemetry.export_aborted" not in _events(logs)
    assert "controller.telemetry.export_close_deferred" in _events(logs)
    release.set()
    await asyncio.sleep(0.05)
    pieces.close()  # the worker finished its piece; now it can be closed
    assert closed.wait(5)
