"""V1.0 G10 (D444): what gRPC aio reports when a write races the peer's end.

Lab L3 (2026-11-10) saw ``stream_ended INTERNAL "Internal error from Core"``.
This pins the real gRPC behaviour behind it, on a real loopback server: when the
server ends a bidirectional stream (here ``ABORTED lease_expired``, what the
controller sends) while the client is writing, the client's write usually fails
with gRPC aio's *local* ``INTERNAL "Internal error from Core"`` - and that status
**replaces** the server's on the call. The agent therefore flags such an end
``write_failed`` (:func:`~meshforge.agent.session.client.write_failed`) instead of
reading it as a controller fault, and still reconnects.

If a gRPC upgrade changes the detail text or stops masking the status, this test
fails and ``CORE_INTERNAL_DETAIL`` must follow.
"""

from __future__ import annotations

import asyncio
import contextlib

import grpc
import pytest

from meshforge.agent.session.client import (
    CORE_INTERNAL_DETAIL,
    StreamEnd,
    write_failed,
)
from meshforge.agent.session.policy import StreamEndAction, stream_end_action

pytestmark = pytest.mark.component

_TRIES = 40


async def _abort_on_first(request_iterator: object, context: object) -> None:
    async for _ in request_iterator:  # type: ignore[attr-defined]
        await context.abort(  # type: ignore[attr-defined]
            grpc.StatusCode.ABORTED, "lease_expired"
        )


@pytest.mark.asyncio
async def test_a_write_racing_the_servers_abort_masks_its_status() -> None:
    server = grpc.aio.server()
    handler = grpc.method_handlers_generic_handler(
        "g10.Race",
        {
            "Stream": grpc.stream_stream_rpc_method_handler(
                _abort_on_first,
                request_deserializer=bytes,
                response_serializer=bytes,
            )
        },
    )
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    seen: list[tuple[grpc.StatusCode, str]] = []
    try:
        for _ in range(_TRIES):
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                call = channel.stream_stream(
                    "/g10.Race/Stream",
                    request_serializer=bytes,
                    response_deserializer=bytes,
                )()
                # a heartbeat/report writer that keeps going; the race either
                # fails the write (masked) or finds the RPC already finished
                with contextlib.suppress(
                    grpc.aio.AioRpcError, grpc.aio.UsageError, asyncio.InvalidStateError
                ):
                    while True:
                        await call.write(b"x" * 64)
                seen.append((await call.code(), await call.details() or ""))
    finally:
        await server.stop(None)
    masked = [s for s in seen if s == (grpc.StatusCode.INTERNAL, CORE_INTERNAL_DETAIL)]
    real = [s for s in seen if s == (grpc.StatusCode.ABORTED, "lease_expired")]
    assert len(masked) + len(real) == len(seen), seen  # nothing else ever
    assert masked, f"no masked end in {_TRIES} races: {seen[:3]}"
    code, details = masked[0]
    end = StreamEnd(code, details, stream_end_action(code, details))
    assert write_failed(end)
    assert end.action is StreamEndAction.RECONNECT  # never a stop: reconnect
