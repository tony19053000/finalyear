"""V0.4 G1 (D40): the agent's requested stop ends its stream with ``Goodbye``.

No network: a scripted stand-in for the gRPC call records every write and the
half-close, and ends the stream (EOF, then status OK) once it has seen the
half-close - what the controller does after a Goodbye. The real stream is covered
in ``tests/component/agent/test_agent_goodbye.py``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import grpc
import pytest
import structlog

from meshforge.agent.identity import load_or_create
from meshforge.agent.session.client import GOODBYE_DETAIL, AgentSessionClient
from meshforge.agent.session.replay import REPORT_PAYLOADS
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings
from meshforge.common.ids import new_command_id, new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


class ScriptedCall:
    """Records writes and the half-close in order; ``read`` blocks until the
    half-close (or a queued controller message), then returns EOF."""

    def __init__(self, *, dead: bool = False) -> None:
        self.events: list[str] = []
        self.written: list[agent_pb2.AgentMessage] = []
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self._dead = dead
        self.cancelled = False
        self.release = asyncio.Event()

    async def write(self, msg: agent_pb2.AgentMessage) -> None:
        if self._dead:
            raise asyncio.InvalidStateError("call already finished")
        self.events.append(msg.WhichOneof("payload") or "")
        self.written.append(msg)

    async def done_writing(self) -> None:
        self.events.append("done_writing")
        self._inbox.put_nowait(grpc.aio.EOF)

    def push(self, msg: agent_pb2.ControllerMessage) -> None:
        self._inbox.put_nowait(msg)

    async def read(self) -> Any:
        return await self._inbox.get()

    async def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.OK

    async def details(self) -> str:
        return ""

    def cancel(self) -> None:
        self.cancelled = True

    def done(self) -> bool:
        return True


@pytest.fixture
def client(tmp_path: Path) -> AgentSessionClient:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data", agent={"controller_address": "127.0.0.1:1"}
    )
    return AgentSessionClient(settings, load_or_create(settings.identity_dir))


async def stop_while_reading(
    client: AgentSessionClient, call: ScriptedCall
) -> asyncio.Task[Any]:
    client._call = call
    client.session_id = new_session_id()
    client.state = AgentState.ONLINE
    client._open_gate().ready.set()
    client._stop_waiter = asyncio.create_task(client._stop.wait())
    reader = asyncio.create_task(client._read_loop(call))
    await asyncio.sleep(0)
    client.request_stop()
    return reader


@pytest.mark.asyncio
async def test_a_requested_stop_says_goodbye_last_then_half_closes(
    client: AgentSessionClient,
) -> None:
    call = ScriptedCall()
    with structlog.testing.capture_logs() as logs:
        reader = await stop_while_reading(client, call)
        end = await asyncio.wait_for(reader, 1.0)
    assert end.code is grpc.StatusCode.OK
    assert call.events == ["goodbye", "done_writing"]
    (bye,) = call.written
    assert bye.goodbye.reason == agent_pb2.Goodbye.REASON_SHUTDOWN
    assert bye.goodbye.detail == GOODBYE_DETAIL
    assert bye.session_id == client.session_id  # the controller fences on it
    assert bye.message_id.startswith("cmd_")
    assert [e for e in logs if e["event"] == "agent.session.goodbye_sent"]


@pytest.mark.asyncio
async def test_nothing_is_written_after_the_goodbye(
    client: AgentSessionClient,
) -> None:
    """A Ping still arriving while closing, a heartbeat, a report: none of them may
    follow the Goodbye onto the stream (it must be the last message)."""
    call = ScriptedCall()
    call.done_writing = _never_half_close(call)  # type: ignore[method-assign]
    reader = await stop_while_reading(client, call)
    for _ in range(50):  # until the Goodbye is out (no wall-clock wait)
        if call.events:
            break
        await asyncio.sleep(0)
    assert call.events == ["goodbye"]
    with pytest.raises(asyncio.InvalidStateError):
        await client._write(
            agent_pb2.AgentMessage(heartbeat=agent_pb2.Heartbeat(sequence=1))
        )
    call.release.set()
    await asyncio.wait_for(reader, 1.0)
    assert call.events == ["goodbye", "done_writing"]


def _never_half_close(call: ScriptedCall) -> Any:
    """Hold the half-close until the test released it, so the test can try to
    write between the Goodbye and the half-close."""
    original = call.done_writing

    async def held() -> None:
        await call.release.wait()
        await original()

    return held


@pytest.mark.asyncio
async def test_a_ping_read_while_closing_is_not_answered_after_goodbye(
    client: AgentSessionClient,
) -> None:
    call = ScriptedCall()
    client.session_id = new_session_id()
    call.push(
        agent_pb2.ControllerMessage(
            message_id=new_command_id(),
            session_id=client.session_id,
            ping=agent_pb2.Ping(ping_id="p"),
        )
    )
    client._writes_closed = True  # the Goodbye went out just before this read
    client._call = call
    client.state = AgentState.ONLINE
    with pytest.raises(asyncio.InvalidStateError):
        await client._handle(await call.read())
    assert call.written == []


@pytest.mark.asyncio
async def test_a_stop_on_a_dead_stream_skips_the_goodbye_and_still_ends(
    client: AgentSessionClient,
) -> None:
    call = ScriptedCall(dead=True)
    with structlog.testing.capture_logs() as logs:
        reader = await stop_while_reading(client, call)
        await asyncio.wait_for(reader, 1.0)
    assert call.written == []
    assert call.events == ["done_writing"]
    assert [e for e in logs if e["event"] == "agent.session.goodbye_not_sent"]


@pytest.mark.asyncio
async def test_goodbye_is_never_buffered_for_replay(client: AgentSessionClient) -> None:
    """A Goodbye replayed onto the next stream would close that new session."""
    assert "goodbye" not in REPORT_PAYLOADS
    with pytest.raises(ValueError, match="not a report"):
        await client.send_report("goodbye", agent_pb2.Goodbye())
    call = ScriptedCall()
    reader = await stop_while_reading(client, call)
    await asyncio.wait_for(reader, 1.0)
    assert all(
        entry.message.WhichOneof("payload") != "goodbye"
        for entry in client.ring.pending()
    )
