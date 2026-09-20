"""gRPC handler for ``AgentControl.Session`` (roadmap 0.3.6).

Per stream::

    agent  --Hello-->  (within hello_timeout_s, else FAILED_PRECONDITION)
           <--HelloAck{accepted, session_id, intervals}--   or {accepted=false} + close
           ... reader task consumes AgentMessages; this handler drains the
               session's outbound queue and yields ControllerMessages ...
    stream ends when the session is closed for any reason (superseded, lease
    expired, protocol error, controller shutdown) or the agent goes away.

The handler owns no session state; everything lives in
:class:`~meshforge.controller.session.manager.SessionManager`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc

from meshforge.common.ids import new_command_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.manager import Rejected, SessionManager

__all__ = ["AgentControlServicer"]

log = get_logger(__name__)

type _Context = grpc.aio.ServicerContext[
    agent_pb2.AgentMessage, agent_pb2.ControllerMessage
]


class AgentControlServicer(agent_pb2_grpc.AgentControlServicer):
    """See module docstring."""

    def __init__(self, manager: SessionManager, *, hello_timeout_s: float) -> None:
        if not hello_timeout_s > 0:
            raise ValueError("hello_timeout_s must be > 0")
        self._manager = manager
        self._hello_timeout_s = hello_timeout_s

    async def Session(  # noqa: N802 - name fixed by the gRPC service definition
        self,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: _Context,
    ) -> AsyncIterator[agent_pb2.ControllerMessage]:
        peer = context.peer()
        first = await self._first_message(request_iterator, context, peer)
        result = await self._manager.admit(first.hello, peer)
        if isinstance(result, Rejected):
            log.info(
                "session.rejected",
                node_id=first.hello.node_id,
                reject_code=agent_pb2.HelloAck.RejectCode.Name(result.reject_code),
                reason=result.reason,
                peer=peer,
            )
            if result.abort_status is not None:
                await context.abort(result.abort_status, result.reason)
            yield agent_pb2.ControllerMessage(
                message_id=new_command_id(),
                hello_ack=agent_pb2.HelloAck(
                    accepted=False,
                    reject_code=result.reject_code,
                    reject_reason=result.reason,
                ),
            )
            return

        live = result.live
        yield result.hello_ack
        self._manager.start_reader(live, request_iterator)
        try:
            while True:
                msg = await live.outbound.get()
                if msg is None:
                    break
                yield msg
        finally:
            # Reached on a clean close (sentinel), on client disconnect (the handler
            # is cancelled) and on any error: the session must never outlive its
            # stream. A session already closed by the manager keeps its reason.
            await self._manager.end_session(live, SessionEndReason.AGENT_CLOSED)

    async def _first_message(
        self,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: _Context,
        peer: str,
    ) -> agent_pb2.AgentMessage:
        try:
            first = await asyncio.wait_for(
                anext(request_iterator), timeout=self._hello_timeout_s
            )
        except TimeoutError:
            log.info(
                "session.hello_timeout", peer=peer, timeout_s=self._hello_timeout_s
            )
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"expected Hello within {self._hello_timeout_s} s",
            )
        except StopAsyncIteration:
            log.info("session.closed_before_hello", peer=peer)
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, "stream closed before Hello"
            )
        if first.WhichOneof("payload") != "hello":
            log.info(
                "session.first_message_not_hello",
                peer=peer,
                payload=first.WhichOneof("payload"),
            )
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "first message on a Session stream must be Hello",
            )
        return first
