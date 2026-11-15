"""V1.0 G11 (CI race): a stop while the HelloAck is in flight still says Goodbye.

The controller may already have accepted the Hello and opened the session when
SIGTERM lands; closing without a Goodbye made it record ``stream_lost`` instead
of ``agent_closed`` (CI: test_sigterm_exits_zero_and_controller_records_agent_closed).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from meshforge.agent.session.client import AgentSessionClient
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


class _Call:
    def __init__(self, first: Any, *, delay: float = 0.0) -> None:
        self._first, self._delay = first, delay

    async def read(self) -> Any:
        await asyncio.sleep(self._delay)
        return self._first


def _client(log: list[str]) -> AgentSessionClient:
    client = object.__new__(AgentSessionClient)
    client.session_id = ""

    async def goodbye() -> None:
        log.append(f"goodbye:{client.session_id}")

    async def close(call: Any) -> None:
        log.append("close")

    client._say_goodbye = goodbye  # type: ignore[method-assign]
    client._close_call = close  # type: ignore[method-assign]
    return client


def _ack(*, accepted: bool) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        hello_ack=agent_pb2.HelloAck(accepted=accepted, session_id="sess_1")
    )


@pytest.mark.asyncio
async def test_an_accepted_ack_after_the_stop_gets_a_goodbye() -> None:
    log: list[str] = []
    await _client(log)._stop_before_ack(_Call(_ack(accepted=True)))
    assert log == ["goodbye:sess_1", "close"]


@pytest.mark.asyncio
async def test_a_rejected_ack_gets_no_goodbye() -> None:
    log: list[str] = []
    await _client(log)._stop_before_ack(_Call(_ack(accepted=False)))
    assert log == ["close"]


@pytest.mark.asyncio
async def test_no_ack_within_the_grace_just_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshforge.agent.session.client as mod

    monkeypatch.setattr(mod, "_CLOSE_GRACE_S", 0.05)
    log: list[str] = []
    await _client(log)._stop_before_ack(_Call(_ack(accepted=True), delay=1.0))
    assert log == ["close"]
