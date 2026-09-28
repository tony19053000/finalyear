"""V0.4 G1 (D40): the Goodbye decision table, on the controller's reader.

Only a ``Goodbye`` carrying the session's own id is a clean close
(``agent_closed`` -> OFFLINE now, decided on receipt). Every other end of the
stream - a normal end of the request iterator included - is ``stream_lost``
(SUSPECT, lingering until the lease edge). In-memory fenced registry and fake
clock from ``test_session_linger``; the reader runs on a stream the test feeds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import structlog
from test_session_linger import admit, make, settle

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.limits import GOODBYE_DETAIL_MAX_LENGTH
from meshforge.controller.session.live import LiveSession

pytestmark = pytest.mark.unit


class FedStream:
    """Request iterator fed by the test; ``end()`` is a normal end (half-close)."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[agent_pb2.AgentMessage | None] = asyncio.Queue()
        self.taken = 0

    def feed(self, msg: agent_pb2.AgentMessage) -> None:
        self._q.put_nowait(msg)

    def end(self) -> None:
        self._q.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[agent_pb2.AgentMessage]:
        return self

    async def __anext__(self) -> agent_pb2.AgentMessage:
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        self.taken += 1
        return item


def goodbye(
    live: LiveSession, *, detail: str = "agent stop requested", session_id: str = ""
) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=session_id or live.session_id,
        goodbye=agent_pb2.Goodbye(
            reason=agent_pb2.Goodbye.REASON_SHUTDOWN, detail=detail
        ),
    )


def heartbeat(live: LiveSession) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        heartbeat=agent_pb2.Heartbeat(sequence=1),
    )


def outbound(live: LiveSession) -> list[agent_pb2.ControllerMessage | None]:
    items: list[agent_pb2.ControllerMessage | None] = []
    while not live.outbound.empty():
        items.append(live.outbound.get_nowait())
    return items


# ------------------------------------------------------------ clean closes


@pytest.mark.asyncio
async def test_goodbye_then_iterator_end_is_agent_closed_offline_now() -> None:
    manager, reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = FedStream()
    task = manager.start_reader(live, stream)
    with structlog.testing.capture_logs() as logs:
        stream.feed(goodbye(live))
        await settle()
        # Decided on receipt - before the stream even ended.
        assert live.ended and live.end_reason is SessionEndReason.AGENT_CLOSED
        stream.end()
        await asyncio.wait_for(task, 0.5)
    assert live.state is NodeState.OFFLINE
    assert not live.lingering and manager.get_lingering(live.node_id) is None
    (bye,) = [e for e in logs if e["event"] == "session.goodbye"]
    assert bye["reason"] == "REASON_SHUTDOWN"
    assert bye["detail"] == "agent stop requested"
    await settle()
    assert ("close_session", live.session_id, SessionEndReason.AGENT_CLOSED, True) in (
        reg.writes
    )


@pytest.mark.asyncio
async def test_goodbye_then_cancel_stays_agent_closed() -> None:
    """The servicer's ``finally``/done-callback closes ``stream_lost`` after a
    cancel; first reason wins, so the announced end keeps its label."""
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = FedStream()
    task = manager.start_reader(live, stream)
    stream.feed(goodbye(live))
    await settle()
    await manager.end_session(live, SessionEndReason.STREAM_LOST)  # the handler
    manager.end_session_nowait(live, SessionEndReason.STREAM_LOST)  # done-callback
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 0.5)
    assert live.end_reason is SessionEndReason.AGENT_CLOSED
    assert live.state is NodeState.OFFLINE and not live.lingering


@pytest.mark.asyncio
async def test_goodbye_is_never_acked() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    outbound(live)  # nothing before
    await manager.handle_message(live, goodbye(live))
    # Only the handler's end-of-stream sentinel: no Ack for the Goodbye.
    assert outbound(live) == [None]


@pytest.mark.asyncio
async def test_nothing_after_a_goodbye_is_read() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = FedStream()
    task = manager.start_reader(live, stream)
    stream.feed(goodbye(live))
    stream.feed(heartbeat(live))
    stream.feed(heartbeat(live))
    await asyncio.wait_for(task, 0.5)  # the reader returns without the stream ending
    assert stream.taken == 1
    assert live.heartbeats == 0
    assert live.end_reason is SessionEndReason.AGENT_CLOSED


@pytest.mark.asyncio
async def test_an_unknown_goodbye_reason_is_still_a_clean_close() -> None:
    """A newer agent may add reasons; the announcement is what counts."""
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    msg = goodbye(live)
    msg.goodbye.reason = 7  # type: ignore[assignment]
    with structlog.testing.capture_logs() as logs:
        await manager.handle_message(live, msg)
    assert live.end_reason is SessionEndReason.AGENT_CLOSED
    (bye,) = [e for e in logs if e["event"] == "session.goodbye"]
    assert bye["reason"] == 7


# ----------------------------------------------------------- abrupt ends


@pytest.mark.asyncio
async def test_iterator_end_without_goodbye_is_stream_lost() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = FedStream()
    task = manager.start_reader(live, stream)
    stream.feed(heartbeat(live))
    with structlog.testing.capture_logs() as logs:
        stream.end()
        await asyncio.wait_for(task, 0.5)
    assert live.end_reason is SessionEndReason.STREAM_LOST
    assert live.state is NodeState.SUSPECT and live.lingering
    assert [e for e in logs if e["event"] == "session.stream_ended_without_goodbye"]


@pytest.mark.asyncio
async def test_cancel_without_goodbye_is_stream_lost() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = FedStream()
    task = manager.start_reader(live, stream)
    stream.feed(heartbeat(live))
    await settle()
    await manager.end_session(live, SessionEndReason.STREAM_LOST)  # the handler
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 0.5)
    assert task.cancelled()
    assert live.end_reason is SessionEndReason.STREAM_LOST
    assert live.state is NodeState.SUSPECT and live.lingering


# ------------------------------------------------------- hostile Goodbyes


@pytest.mark.asyncio
async def test_a_goodbye_with_a_stale_session_id_is_ignored_and_counted() -> None:
    """A zombie stream's Goodbye (another session's id) closes nothing."""
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    before = manager.stale_messages_total
    await manager.handle_message(
        live, goodbye(live, session_id="ses_01J00000000000000000000000")
    )
    assert not live.ended
    assert live.stale_messages == 1
    assert manager.stale_messages_total == before + 1
    assert manager.get(live.node_id) is live


@pytest.mark.asyncio
async def test_a_goodbye_detail_at_the_bound_is_accepted() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    await manager.handle_message(
        live, goodbye(live, detail="x" * GOODBYE_DETAIL_MAX_LENGTH)
    )
    assert live.end_reason is SessionEndReason.AGENT_CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    ["x" * (GOODBYE_DETAIL_MAX_LENGTH + 1), "bye\x1b[2J", "bye\u202e", "a\nb"],
    ids=["over-bound", "escape", "bidi-override", "newline"],
)
async def test_a_bad_goodbye_detail_is_a_protocol_error_not_a_clean_close(
    detail: str,
) -> None:
    """Refused, never cleaned (D22 rule): the session ends ``protocol_error`` -
    abrupt, so SUSPECT and the lease lingers (the safe direction)."""
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = FedStream()
    task = manager.start_reader(live, stream)
    with structlog.testing.capture_logs() as logs:
        stream.feed(goodbye(live, detail=detail))
        await asyncio.wait_for(task, 0.5)
    assert live.end_reason is SessionEndReason.PROTOCOL_ERROR
    assert live.state is NodeState.SUSPECT and live.lingering
    (err,) = [e for e in logs if e["event"] == "session.protocol_error"]
    assert "Goodbye.detail" in err["error"]
    assert detail not in err["error"]  # the value is never echoed
    assert not [e for e in logs if e["event"] == "session.goodbye"]


@pytest.mark.asyncio
async def test_a_goodbye_without_message_id_is_a_protocol_error() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = FedStream()
    task = manager.start_reader(live, stream)
    msg = goodbye(live)
    msg.message_id = ""
    stream.feed(msg)
    await asyncio.wait_for(task, 0.5)
    assert live.end_reason is SessionEndReason.PROTOCOL_ERROR
