"""V0.7 G6 fix round 1 (gate): ``DockerRuntime._owned`` - a blocking Docker
call whose caller is cancelled never drops what it made.

Pure logic (no daemon): the call runs in a worker thread and cannot be
interrupted; when the awaiting coroutine is cancelled meanwhile, the result
(a client, an attach socket, a created container) is handed to ``release``
once the call returns, as a tracked background task. Not cancelled: the
result is returned and ``release`` is never called. A call that raises
released nothing.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _runtime() -> Any:
    return DockerRuntime(DockerConfig(agent_label="unit-owned"))


async def _drain(runtime: Any) -> None:
    for _ in range(200):
        if not runtime._background:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("late releases never finished")


async def test_the_result_is_returned_and_not_released_without_a_cancel() -> None:
    runtime = _runtime()
    released: list[object] = []
    made = object()
    assert await runtime._owned(lambda: made, released.append) is made
    await asyncio.sleep(0.05)
    assert released == [] and not runtime._background


async def test_a_cancelled_caller_gets_its_late_result_released() -> None:
    runtime = _runtime()
    gate = threading.Event()
    entered = threading.Event()
    released: list[object] = []
    made = object()

    def call() -> object:
        entered.set()
        gate.wait(10)
        return made

    task = asyncio.ensure_future(runtime._owned(call, released.append))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released == []  # the call has not returned yet
    gate.set()
    for _ in range(200):
        if released:
            break
        await asyncio.sleep(0.01)
    assert released == [made]
    await _drain(runtime)


async def test_a_call_that_fails_after_the_cancel_releases_nothing() -> None:
    runtime = _runtime()
    gate = threading.Event()
    entered = threading.Event()
    released: list[object] = []

    def call() -> object:
        entered.set()
        gate.wait(10)
        raise OSError("daemon went away")

    task = asyncio.ensure_future(runtime._owned(call, released.append))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.sleep(0.1)
    assert released == [] and not runtime._background


async def test_an_error_without_a_cancel_reaches_the_caller() -> None:
    runtime = _runtime()

    def call() -> object:
        raise OSError("no daemon")

    with pytest.raises(OSError, match="no daemon"):
        await runtime._owned(call, lambda _made: None)


async def test_a_failing_release_is_logged_not_raised() -> None:
    runtime = _runtime()
    gate = threading.Event()
    entered = threading.Event()

    def call() -> object:
        entered.set()
        gate.wait(10)
        return object()

    def release(_made: object) -> None:
        raise RuntimeError("cannot release")

    task = asyncio.ensure_future(runtime._owned(call, release))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await _drain(runtime)  # the background task ended; its failure was logged
