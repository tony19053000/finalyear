"""gRPC handler for ``AgentControl.Session`` (roadmap 0.3.6).

Per stream::

    agent  --Hello-->  (within hello_timeout_s, else FAILED_PRECONDITION)
           <--HelloAck{accepted, session_id, intervals}--   or {accepted=false} + close
           ... reader task consumes AgentMessages; this handler drains the
               session's outbound queue and yields ControllerMessages ...
    stream ends when the session is closed for any reason (superseded, lease
    expired, protocol error, controller shutdown) or the agent goes away.

Final status of an accepted stream: ``OK`` when the close was clean
(``agent_closed``, ``controller_shutdown``); ``ABORTED`` with the end reason as
the detail for everything else (``superseded``, ``lease_expired``,
``protocol_error``, ``internal_error``, ``stream_lost``) so an agent can tell "I
was replaced / timed out / broke the contract" from a normal end without a second
round-trip.

**Who labels the end (D19/D40).** The manager's reader labels the ends it can
see: a ``Goodbye`` (``agent_closed`` - the only clean close an agent can make), a
protocol/internal error, and a request stream that ended without a Goodbye
(``stream_lost``). This handler labels the one only it can see - the call being
**cancelled** (the agent's call cancelled, its channel closed, its process killed,
the TCP path torn down by keepalive): grpc.aio cancels the handler task, and the
``finally`` below closes the session ``stream_lost``. Because ``_close`` is
idempotent and the first reason wins, the ``finally`` can never relabel a session
that was already closed for a real reason (a Goodbye followed by a cancel stays
``agent_closed``). A done-callback on the RPC closes it ``stream_lost`` too, in
case the handler was cancelled while suspended at a ``yield`` and its ``finally``
runs late (the lease timer is the last backstop).

Hello outcomes that are not a ``HelloAck``: a malformed ``node_id`` or a
``session_epoch`` outside ``[0, MAX_SESSION_EPOCH]`` (see
:mod:`meshforge.controller.session.epoch`) end the stream ``INVALID_ARGUMENT`` with
nothing persisted; a revoked node ``PERMISSION_DENIED``; a controller that is
shutting down ``UNAVAILABLE``. A registry/store failure *during* admission (database
unreachable, constraint violation...) is our fault, not the agent's: it is logged
``session.admit_failed`` and the stream ends ``INTERNAL`` - never an unhandled
exception in the handler.

The handler owns no session state; everything lives in
:class:`~meshforge.controller.session.manager.SessionManager`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import grpc

from meshforge.common.errors import MeshForgeError
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

_CLEAN_END_REASONS: frozenset[SessionEndReason] = frozenset(
    {SessionEndReason.AGENT_CLOSED, SessionEndReason.CONTROLLER_SHUTDOWN}
)
"""End reasons that finish the stream ``OK``; every other one is ``ABORTED``."""


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
        try:
            result = await self._manager.admit(first.hello, peer)
        except MeshForgeError as exc:
            # RegistryError / StoreError / any typed failure of our own layers.
            # The fields name only the operation and the error class; the
            # traceback carries the typed wrapper's message (never a DSN or
            # credentials - see registry._typed).
            log.exception(
                "session.admit_failed",
                node_id=first.hello.node_id,
                peer=peer,
                cause=type(exc).__name__,
                failure_type=exc.failure_type.value,
                retryable=exc.retryable,
                operation=exc.details.get("operation"),
            )
            await context.abort(
                grpc.StatusCode.INTERNAL,
                f"controller could not admit the session ({type(exc).__name__})",
            )
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
        manager = self._manager

        def on_rpc_done(_ctx: object) -> None:
            manager.end_session_nowait(live, SessionEndReason.STREAM_LOST)

        # grpc-stubs types the callback as a nominal (non-Protocol) class no
        # function can satisfy; at runtime any callable taking the context works.
        context.add_done_callback(cast("Any", on_rpc_done))
        yield result.hello_ack
        manager.start_reader(live, request_iterator)
        try:
            while True:
                msg = await live.outbound.get()
                if msg is None:
                    break
                yield msg
        finally:
            # After the sentinel the session is already closed and keeps its
            # reason (first wins). Anything else that gets here - the handler
            # cancelled because the call was, an error writing to the transport -
            # is an abrupt end: never ``agent_closed`` (D19).
            await manager.end_session(live, SessionEndReason.STREAM_LOST)
        if live.end_reason not in _CLEAN_END_REASONS:
            reason = live.end_reason.value if live.end_reason else "unknown"
            await context.abort(grpc.StatusCode.ABORTED, reason)

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
