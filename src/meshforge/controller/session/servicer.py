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
``finally`` below closes the session ``stream_lost``. Because a close is
idempotent and the first reason wins, the ``finally`` can never relabel a session
that was already closed for a real reason (a Goodbye followed by a cancel stays
``agent_closed``). A done-callback on the RPC closes it ``stream_lost`` too, in
case the handler was cancelled while suspended at a ``yield`` and its ``finally``
runs late (the lease timer is the last backstop).

**Identity (V0.7 G3, D137).** In ``mtls`` mode every call on the port already
carries a verified fabric node certificate (TLS + :class:`~.peer_cert.NodeCertInterceptor`);
the Hello's ``node_id`` must be the certificate's, else the stream ends
``UNAUTHENTICATED`` with ``meshforge-reason: node_id_mismatch`` and nothing is
persisted. Admission refuses an older certificate of a re-keyed node
(``cert_superseded``) and records the certificate of a node that has none on
record.

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
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import grpc

from meshforge.common.errors import InvalidIdError, MeshForgeError
from meshforge.common.ids import new_command_id, validate_node_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.admission import NODE_REVOKED, REVOKED_DETAIL
from meshforge.controller.session.manager import Rejected, SessionManager
from meshforge.controller.session.peer_cert import (
    REASON_METADATA_KEY,
    PeerCert,
    PeerCertError,
    PeerCertReason,
    abort_with_reason,
)

__all__ = ["AgentControlServicer"]

log = get_logger(__name__)

type _Context = grpc.aio.ServicerContext[
    agent_pb2.AgentMessage, agent_pb2.ControllerMessage
]

_CLEAN_END_REASONS: frozenset[SessionEndReason] = frozenset(
    {SessionEndReason.AGENT_CLOSED, SessionEndReason.CONTROLLER_SHUTDOWN}
)
"""End reasons that finish the stream ``OK``; every other one is ``ABORTED``."""


def _well_formed(node_id: str) -> bool:
    try:
        validate_node_id(node_id)
    except InvalidIdError:
        return False
    return True


class AgentControlServicer(agent_pb2_grpc.AgentControlServicer):
    """See module docstring."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        hello_timeout_s: float,
        identify: Callable[[Any], PeerCert] | None = None,
    ) -> None:
        """``identify`` (mtls mode, V0.7 G3): the caller's verified client
        certificate (:func:`~.peer_cert.peer_cert`); ``None`` only in
        ``insecure-dev``, where ``Hello.node_id`` is taken as claimed."""
        if not hello_timeout_s > 0:
            raise ValueError("hello_timeout_s must be > 0")
        self._manager = manager
        self._hello_timeout_s = hello_timeout_s
        self._identify = identify

    async def Session(  # noqa: N802 - name fixed by the gRPC service definition
        self,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: _Context,
    ) -> AsyncIterator[agent_pb2.ControllerMessage]:
        peer = context.peer()
        first = await self._first_message(request_iterator, context, peer)
        cert = await self._certified(first.hello, context, peer)
        try:
            result = await self._manager.admit(first.hello, peer, cert=cert)
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
                if result.reason_code is not None:
                    context.set_trailing_metadata(
                        ((REASON_METADATA_KEY, result.reason_code),)
                    )
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
        if live.end_reason is SessionEndReason.REVOKED:
            # V0.7 G3 (D138): the agent stops for good (FATAL_CONFIG) and says
            # how to come back; nothing it sends is applied any more.
            context.set_trailing_metadata(((REASON_METADATA_KEY, NODE_REVOKED),))
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, REVOKED_DETAIL)
        if live.end_reason not in _CLEAN_END_REASONS:
            reason = live.end_reason.value if live.end_reason else "unknown"
            await context.abort(grpc.StatusCode.ABORTED, reason)

    async def _certified(
        self, hello: agent_pb2.Hello, context: _Context, peer: str
    ) -> PeerCert | None:
        """D137: the certificate's node must be the node the Hello names."""
        if self._identify is None:
            return None
        try:
            cert = self._identify(context)
        except PeerCertError as error:  # the interceptor normally refused it already
            await abort_with_reason(context, error)
            raise AssertionError("unreachable: abort raises") from None
        if hello.node_id != cert.node_id and _well_formed(hello.node_id):
            # (A malformed node_id is refused INVALID_ARGUMENT by admission,
            # before anything is persisted, as for every peer.)
            log.warning(
                "session.node_id_mismatch",
                claimed_node_id=hello.node_id[:64],
                node_id=cert.node_id,
                cert_serial=cert.serial,
                peer=peer,
            )
            await abort_with_reason(
                context,
                PeerCertError(
                    PeerCertReason.NODE_ID_MISMATCH,
                    "Hello.node_id is not the node named by this connection's "
                    "certificate",
                ),
            )
        return cert

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
