"""V1.0 G10 (D450): a Docker env build / setup step that ran out of disk is
``disk_full`` (the installer's own ENOSPC text in its output), as the venv
runtime has done since D116 - not ``env_error``, which would mark the env
known-bad on the node for a condition of its disk."""

from __future__ import annotations

import asyncio
import threading

import pytest

from meshforge.agent.execution import docker_runtime as dr
from meshforge.agent.execution._pump import OutputPump
from meshforge.agent.execution.runtime import Stream
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

PIP_ENOSPC = (
    b"ERROR: Could not install packages due to an OSError: "
    b"[Errno 28] No space left on device\n"
)


async def _sink(stream: Stream, data: bytes) -> None:
    del stream, data


@pytest.mark.asyncio
async def test_the_pump_keeps_a_bounded_tail_from_threads_and_the_loop() -> None:
    pump = OutputPump(_sink, name="t")
    pump.start()
    assert pump.tail() == b""
    await pump.put(Stream.STDOUT, b"before keep_tail\n")
    assert pump.tail() == b""  # nothing kept until asked
    pump.keep_tail(32)
    await pump.put(Stream.STDOUT, b"x" * 40)
    stop = threading.Event()
    sent = await asyncio.to_thread(pump.put_from_thread, Stream.STDERR, b"END", stop)
    assert sent
    assert pump.tail() == b"x" * 29 + b"END"
    pump.keep_tail(32)  # a fresh tail per step
    assert pump.tail() == b""
    await pump.close(1.0)


@pytest.mark.asyncio
async def test_enospc_in_the_step_output_is_disk_full() -> None:
    pump = OutputPump(_sink, name="t")
    pump.start()
    pump.keep_tail(4096)
    await pump.put(Stream.STDERR, b"Processing /wheels/x.whl\n" + PIP_ENOSPC)
    with pytest.raises(dr._FaultError) as info:
        dr._raise_if_disk_full(pump, 1)
    assert info.value.failure_type is FailureType.DISK_FULL
    assert info.value.reason == "disk_full"
    await pump.close(1.0)


@pytest.mark.asyncio
async def test_another_install_failure_is_left_to_env_error() -> None:
    pump = OutputPump(_sink, name="t")
    pump.start()
    pump.keep_tail(4096)
    await pump.put(Stream.STDERR, b"ERROR: No matching distribution found for x\n")
    dr._raise_if_disk_full(pump, 1)  # no raise: the caller types it env_error
    await pump.close(1.0)
