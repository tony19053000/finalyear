"""Hand-rolled ``AgentControl.Session`` client for controller component/cluster tests.

There is no agent runtime yet (V0.3 G3); until then tests drive the controller with
this thin wrapper over the generated ``AgentControlStub``: one bidi call, one
background reader task, explicit Hello/heartbeat/pong helpers, and the final gRPC
status of the stream. Nothing here is a mock - every message crosses a real socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import grpc

from meshforge import __version__
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc, common_pb2

if TYPE_CHECKING:
    from meshforge.controller.registry import Registry, SessionEndReason

__all__ = [
    "FakeAgent",
    "free_port_pair",
    "make_hello",
    "poll_until",
    "wait_session_end_reason",
]


def make_hello(node_id: str, epoch: int, **overrides: Any) -> agent_pb2.Hello:
    fields: dict[str, Any] = {
        "node_id": node_id,
        "protocol_version": common_pb2.PROTOCOL_VERSION_1,
        "agent_version": __version__,
        "hostname": "component-test-laptop",
        "session_epoch": epoch,
        "os": "TestOS 1.0",
        "platform": "test-x86_64",
        "hardware": agent_pb2.HardwareSummary(cpu_model="test-cpu", cpu_cores=8),
    }
    fields.update(overrides)
    return agent_pb2.Hello(**fields)


@dataclass(eq=False)
class FakeAgent:
    """One ``AgentControl.Session`` stream driven by hand.

    ``hello()`` writes the Hello and returns the first controller message. After
    that ``start_reading()`` owns the read side: it collects every message, answers
    ``Ping`` with ``Pong`` (unless ``auto_pong`` is False) and sets ``ended`` when
    the stream finishes (EOF or error; ``end_code`` says how).
    """

    channel: Any
    """A ``grpc.aio.Channel``, or a ``mf_fabric.NodeChannels`` pool."""
    node_id: str = field(default_factory=new_node_id)
    epoch: int = 1
    auto_pong: bool = True
    session_id: str = ""
    received: list[agent_pb2.ControllerMessage] = field(default_factory=list)
    pongs_sent: int = 0
    heartbeats_sent: int = 0
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    end_code: grpc.StatusCode | None = None
    end_details: str = ""
    _call: Any = None
    # grpc.aio allows one in-flight write per call; the heartbeat loop and the
    # auto-pong in the reader both write, so writes are serialised.
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _reader: asyncio.Task[None] | None = None
    _hb: asyncio.Task[None] | None = None
    _hb_done: bool = False

    def open(self) -> None:
        channel = self.channel
        # V0.7 G3: a ``mf_fabric.NodeChannels`` pool hands out one mutual-TLS
        # channel per node_id (a channel carries one client certificate).
        for_node = getattr(channel, "for_node", None)
        if callable(for_node):
            channel = for_node(self.node_id)
        stub = agent_pb2_grpc.AgentControlStub(channel)
        self._call = stub.Session()

    async def write(self, msg: agent_pb2.AgentMessage) -> None:
        assert self._call is not None
        async with self._write_lock:
            await self._call.write(msg)

    async def read(self) -> agent_pb2.ControllerMessage | None:
        """One controller message, or None at EOF (records the final status)."""
        assert self._call is not None
        try:
            msg = await self._call.read()
        except grpc.aio.AioRpcError as exc:  # the controller aborted the stream
            self.end_code = exc.code()
            self.end_details = exc.details() or ""
            self.ended.set()
            return None
        if msg is grpc.aio.EOF:
            self.end_code = await self._call.code()
            self.end_details = await self._call.details()
            self.ended.set()
            return None
        assert isinstance(msg, agent_pb2.ControllerMessage)
        self.received.append(msg)
        return msg

    async def hello(self, **overrides: Any) -> agent_pb2.ControllerMessage | None:
        if self._call is None:
            self.open()
        await self.write(
            agent_pb2.AgentMessage(
                message_id=new_command_id(),
                hello=make_hello(self.node_id, self.epoch, **overrides),
            )
        )
        reply = await self.read()
        if reply is not None and reply.WhichOneof("payload") == "hello_ack":
            self.session_id = reply.hello_ack.session_id
        return reply

    async def connect(self, **overrides: Any) -> agent_pb2.HelloAck:
        """Hello (with optional field overrides) and assert acceptance; starts the
        background reader."""
        reply = await self.hello(**overrides)
        assert reply is not None, (self.end_code, self.end_details)
        assert reply.WhichOneof("payload") == "hello_ack", reply
        assert reply.hello_ack.accepted, reply.hello_ack
        self.start_reading()
        return reply.hello_ack

    def start_reading(self) -> None:
        assert self._reader is None
        self._reader = asyncio.create_task(
            self._read_loop(), name=f"fake-agent-reader:{self.node_id}"
        )

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = await self.read()
                if msg is None:
                    return
                if msg.WhichOneof("payload") == "ping" and self.auto_pong:
                    await self.pong(msg.ping.ping_id)
        except grpc.aio.AioRpcError as exc:
            self.end_code = exc.code()
            self.end_details = exc.details() or ""
            self.ended.set()

    async def pong(self, ping_id: str) -> None:
        # The controller pings right after HelloAck; if close() half-closed the
        # call in the meantime the write is moot, not an error (review B2).
        with contextlib.suppress(asyncio.InvalidStateError, grpc.aio.UsageError):
            await self.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    session_id=self.session_id,
                    pong=agent_pb2.Pong(ping_id=ping_id),
                )
            )
            self.pongs_sent += 1

    async def heartbeat(self, *, session_id: str | None = None) -> str:
        self.heartbeats_sent += 1
        message_id = new_command_id()
        await self.write(
            agent_pb2.AgentMessage(
                message_id=message_id,
                session_id=self.session_id if session_id is None else session_id,
                heartbeat=agent_pb2.Heartbeat(sequence=self.heartbeats_sent),
            )
        )
        return message_id

    async def goodbye(
        self,
        *,
        detail: str = "fake agent stop",
        session_id: str | None = None,
        reason: int = agent_pb2.Goodbye.REASON_SHUTDOWN,
    ) -> str:
        """Write a ``Goodbye`` (V0.4 D40): the real agent's last message on stop."""
        message_id = new_command_id()
        await self.write(
            agent_pb2.AgentMessage(
                message_id=message_id,
                session_id=self.session_id if session_id is None else session_id,
                goodbye=agent_pb2.Goodbye(reason=reason, detail=detail),  # type: ignore[arg-type]
            )
        )
        return message_id

    def start_heartbeats(self, interval_s: float) -> None:
        assert self._hb is None

        async def loop() -> None:
            while not self.ended.is_set() and not self._hb_done:
                try:
                    await self.heartbeat()
                except (asyncio.InvalidStateError, grpc.aio.UsageError):
                    return  # the call ended under us (aborted / half-closed)
                except grpc.aio.AioRpcError:
                    # The controller ended the call (e.g. ``superseded``) while a
                    # write was in flight: grpc core then reports the failed send
                    # as INTERNAL "execute_batch" instead of a usage error. The
                    # stream is over either way; the test reads its real end.
                    return
                await asyncio.sleep(interval_s)

        self._hb = asyncio.create_task(loop(), name=f"fake-agent-hb:{self.node_id}")

    def stop_heartbeats(self) -> None:
        if self._hb is not None:
            self._hb.cancel()

    async def finish_heartbeats(self) -> None:
        """Stop the heartbeat loop **without** cancelling a write in flight
        (cancelling ``call.write`` cancels the whole call - the controller
        would then see ``stream_lost``): the loop ends after its current beat."""
        self._hb_done = True
        if self._hb is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._hb

    def messages(self, payload: str) -> list[agent_pb2.ControllerMessage]:
        return [m for m in self.received if m.WhichOneof("payload") == payload]

    async def wait_ended(self, timeout_s: float = 5.0) -> None:
        await asyncio.wait_for(self.ended.wait(), timeout_s)

    async def close(
        self,
        *,
        graceful: bool = False,
        timeout_s: float = 3.0,
        half_close_only: bool = False,
    ) -> None:
        """Stop own tasks, half-close, then end the call.

        ``graceful=False`` (teardown): cancel right after the half-close, no
        Goodbye - the controller records ``stream_lost``, not ``agent_closed``.

        ``graceful=True``: what the real agent's ``request_stop`` does (V0.4 D40) -
        write ``Goodbye``, half-close and keep reading until the controller's final
        status (at most ``timeout_s``), so the controller records ``agent_closed``
        and writes OFFLINE. ``half_close_only=True`` is an *old* agent's graceful
        stop (half-close and wait, no Goodbye): since D40 that is ``stream_lost``.

        Order matters (review B2): the reader (and its auto-pong) is stopped
        *before* ``done_writing()``; the other way round an auto-pong could race
        into the half-closed call and escape the reader as ``InvalidStateError``.
        """
        self.stop_heartbeats()
        if self._hb is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._hb
        if self._reader is not None and not graceful:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, grpc.aio.AioRpcError):
                await self._reader
        if graceful and not half_close_only and self._call is not None:
            with contextlib.suppress(Exception):
                await self.goodbye()
        if self._call is not None:
            with contextlib.suppress(Exception):
                await self._call.done_writing()
        graceful = graceful or half_close_only
        if graceful and self._call is not None:
            with contextlib.suppress(TimeoutError):
                if self._reader is not None:
                    # The reader keeps answering pings until the status arrives.
                    await asyncio.wait_for(self.ended.wait(), timeout_s)
                else:
                    await asyncio.wait_for(self._drain_to_status(), timeout_s)
            if self._reader is not None and not self._reader.done():
                self._reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, grpc.aio.AioRpcError):
                    await self._reader
        if self._call is not None and not self._call.done():
            self._call.cancel()

    async def _drain_to_status(self) -> None:
        while await self.read() is not None:
            pass

    async def abort(self) -> None:
        """Cancel the call **without** a half-close: the agent's end of an abrupt
        loss (D19) - the controller must record ``stream_lost`` and go SUSPECT."""
        self.stop_heartbeats()
        if self._hb is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._hb
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, grpc.aio.AioRpcError):
                await self._reader
        if self._call is not None and not self._call.done():
            self._call.cancel()


async def poll_until(
    predicate: Any, *, timeout_s: float, interval_s: float = 0.02
) -> float:
    """Await ``predicate()`` (async) until truthy; return seconds it took."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while True:
        if await predicate():
            return loop.time() - start
        if loop.time() - start > timeout_s:
            raise AssertionError(f"condition not met within {timeout_s} s")
        await asyncio.sleep(interval_s)


async def wait_session_end_reason(
    registry: Registry, session_id: str, *, timeout_s: float = 5.0
) -> SessionEndReason:
    """The ``node_session`` row's ``end_reason`` once the row is closed.

    The persistent half of a session end trails everything else a test can
    observe: the node's state write (OFFLINE / SUSPECT) is committed first and
    ``close_session`` is a second transaction behind it on the session's write
    chain (D19 order, ``controller/session/ends.py``), and the gRPC status the
    agent sees is sent before either. So the node can read OFFLINE, or the agent
    can have its status, while ``end_reason`` is still null (V1.0 G8 F2). Wait
    for the close, then judge the reason it was closed with.
    """
    seen: list[SessionEndReason] = []

    async def closed() -> bool:
        rec = await registry.get_session(session_id)
        if rec is None or rec.end_reason is None:
            return False
        seen.append(rec.end_reason)
        return True

    await poll_until(closed, timeout_s=timeout_s, interval_s=0.01)
    return seen[-1]


def free_port_pair() -> tuple[int, int]:
    """Two loopback TCP ports that were free a moment ago (for a controller
    *subprocess*, which cannot report an ephemeral port back to the test)."""
    ports: list[int] = []
    for _ in range(2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            ports.append(int(s.getsockname()[1]))
    return ports[0], ports[1]
