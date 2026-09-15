"""Component test for task 0.1.5: the generated code is actually *usable* to
build a real gRPC service, not just importable/serializable.

Runs a real ``grpc.aio`` server on a real localhost TCP port (an ephemeral
port, no shared state) implementing both services declared in the protos:

* ``AgentControl.Session`` -- a bidirectional stream. The "client" (a real
  gRPC channel, not an in-process fake) sends a ``Hello`` envelope; the server
  replies with an ``Ack``. The client then sends an ``Ack`` for a command
  (simulating an agent acking a controller-pushed command); the server reads
  it and records it.
* ``GpuService.OpenSession`` -- a plain unary RPC.

This exercises: service registration, oneof payload dispatch on both ends,
streaming in both directions, and the ``_grpc.py`` server/stub base classes
actually working end-to-end over a socket (not just existing as Python
objects). Never touches ``src/``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Final

import grpc
import pytest
import pytest_asyncio

from meshforge.common.proto.meshforge.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    common_pb2,
    gpu_pb2,
    gpu_pb2_grpc,
)

pytestmark = pytest.mark.component

_HELLO_NODE_ID: Final = "node-usability-test"
_COMMAND_ID: Final = "cmd-42"


class _AgentControlServicer(agent_pb2_grpc.AgentControlServicer):
    """Minimal real server: Ack a Hello, and record any Ack it receives."""

    def __init__(self) -> None:
        self.received_hello: agent_pb2.Hello | None = None
        self.received_ack: agent_pb2.Ack | None = None

    async def Session(  # noqa: N802 - name fixed by the gRPC service definition
        self,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext[
            agent_pb2.AgentMessage, agent_pb2.ControllerMessage
        ],
    ) -> AsyncIterator[agent_pb2.ControllerMessage]:
        async for msg in request_iterator:
            which = msg.WhichOneof("payload")
            if which == "hello":
                self.received_hello = msg.hello
                yield agent_pb2.ControllerMessage(
                    message_id="server-ack-1",
                    ack=agent_pb2.Ack(acked_message_id=msg.message_id),
                )
            elif which == "ack":
                self.received_ack = msg.ack
                # No reply needed; the agent acked a command we "sent" earlier.
            else:  # pragma: no cover - not exercised by this test
                raise AssertionError(f"unexpected payload: {which}")


class _GpuServiceServicer(gpu_pb2_grpc.GpuServiceServicer):
    async def OpenSession(  # noqa: N802 - name fixed by the gRPC service definition
        self,
        request: gpu_pb2.OpenSessionRequest,
        context: grpc.aio.ServicerContext[
            gpu_pb2.OpenSessionRequest, gpu_pb2.OpenSessionResponse
        ],
    ) -> gpu_pb2.OpenSessionResponse:
        if request.protocol_version != common_pb2.PROTOCOL_VERSION_1:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "bad protocol version"
            )
        return gpu_pb2.OpenSessionResponse(
            session_id="session-1", granted_vram_bytes=request.requested_vram_bytes
        )


@pytest_asyncio.fixture
async def running_server() -> AsyncIterator[tuple[str, _AgentControlServicer]]:
    server = grpc.aio.server()
    agent_servicer = _AgentControlServicer()
    agent_pb2_grpc.add_AgentControlServicer_to_server(agent_servicer, server)
    # The stubs mark every RPC abstract; the generated runtime base class answers
    # the ones not overridden with UNIMPLEMENTED, which is all this test needs.
    gpu_servicer = _GpuServiceServicer()  # type: ignore[abstract]
    gpu_pb2_grpc.add_GpuServiceServicer_to_server(gpu_servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port != 0, "server failed to bind an ephemeral localhost port"
    await server.start()
    try:
        yield f"127.0.0.1:{port}", agent_servicer
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_agent_control_session_bidi_hello_ack_over_real_socket(
    running_server: tuple[str, _AgentControlServicer],
) -> None:
    address, servicer = running_server
    async with grpc.aio.insecure_channel(address) as channel:
        stub = agent_pb2_grpc.AgentControlStub(channel)
        # Manual write()/read() (rather than a request-iterator) so the test can
        # interleave a real request and a real reply on the same bidi stream
        # without two concurrent readers of the call object.
        call = stub.Session()

        await call.write(
            agent_pb2.AgentMessage(
                message_id="hello-1",
                hello=agent_pb2.Hello(
                    node_id=_HELLO_NODE_ID,
                    protocol_version=common_pb2.PROTOCOL_VERSION_1,
                    agent_version="0.1.0.dev0",
                    hostname="test-laptop",
                ),
            )
        )
        reply = await call.read()
        assert isinstance(reply, agent_pb2.ControllerMessage), reply
        assert reply.WhichOneof("payload") == "ack"
        assert reply.ack.acked_message_id == "hello-1"

        # Agent acks a controller command on the same stream.
        await call.write(
            agent_pb2.AgentMessage(
                message_id="ack-1", ack=agent_pb2.Ack(acked_message_id=_COMMAND_ID)
            )
        )
        await call.done_writing()
        # Server sends no reply to an Ack; the stream must simply close cleanly.
        end = await call.read()
        assert end == grpc.aio.EOF

    assert servicer.received_hello is not None
    assert servicer.received_hello.node_id == _HELLO_NODE_ID
    assert servicer.received_ack is not None
    assert servicer.received_ack.acked_message_id == _COMMAND_ID


@pytest.mark.asyncio
async def test_gpu_service_open_session_unary_over_real_socket(
    running_server: tuple[str, _AgentControlServicer],
) -> None:
    address, _servicer = running_server
    async with grpc.aio.insecure_channel(address) as channel:
        stub = gpu_pb2_grpc.GpuServiceStub(channel)
        response = await stub.OpenSession(
            gpu_pb2.OpenSessionRequest(
                protocol_version=common_pb2.PROTOCOL_VERSION_1,
                session_token="tok",
                requested_vram_bytes=1024,
            )
        )
    assert response.session_id == "session-1"
    assert response.granted_vram_bytes == 1024


@pytest.mark.asyncio
async def test_gpu_service_rejects_bad_protocol_version_with_real_grpc_error(
    running_server: tuple[str, _AgentControlServicer],
) -> None:
    address, _servicer = running_server
    async with grpc.aio.insecure_channel(address) as channel:
        stub = gpu_pb2_grpc.GpuServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.OpenSession(
                gpu_pb2.OpenSessionRequest(
                    protocol_version=common_pb2.PROTOCOL_VERSION_UNSPECIFIED,
                    session_token="tok",
                )
            )
    assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_two_concurrent_agent_sessions_are_independent(
    running_server: tuple[str, _AgentControlServicer],
) -> None:
    """Two clients holding independent bidi streams at once must not cross-talk
    (each stream is its own request_iterator/generator instance server-side)."""
    address, _servicer = running_server

    async def one_hello(
        node_id: str, channel: grpc.aio.Channel
    ) -> agent_pb2.ControllerMessage:
        stub = agent_pb2_grpc.AgentControlStub(channel)

        async def messages() -> AsyncIterator[agent_pb2.AgentMessage]:
            yield agent_pb2.AgentMessage(
                message_id=f"hello-{node_id}",
                hello=agent_pb2.Hello(
                    node_id=node_id,
                    protocol_version=common_pb2.PROTOCOL_VERSION_1,
                    agent_version="0.1.0.dev0",
                    hostname=node_id,
                ),
            )

        call = stub.Session(messages())
        async for reply in call:
            return reply
        raise AssertionError("server closed the stream without replying")

    async with (
        grpc.aio.insecure_channel(address) as channel_a,
        grpc.aio.insecure_channel(address) as channel_b,
    ):
        reply_a, reply_b = await asyncio.gather(
            one_hello("node-a", channel_a), one_hello("node-b", channel_b)
        )
    assert reply_a.ack.acked_message_id == "hello-node-a"
    assert reply_b.ack.acked_message_id == "hello-node-b"
