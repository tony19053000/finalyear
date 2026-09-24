"""V0.3 G6 (D21): Reconcile stays first even with a suspension point in the window.

``test_reconnect_reconcile_first.py`` pins the order with the window as it is in
production (no ``await`` in it). This test *adds* one - the client's ``on_online``
test hook sleeps for longer than a heartbeat interval between the ONLINE
transition and the Reconcile, while a reporter that is always runnable keeps
calling ``send_report`` - and asserts, from what the **controller** received, that
the first message of the reconnect is still ``Reconcile``. Before D21 that edit
lost telemetry silently; now the gate makes it harmless. The reconnect is forced
by the controller ending the session while it stays up (so the in-memory resource
tracker is not reset - the case the G4 harness could not express).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from mf_agent_harness import make_agent_settings
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.session.manager import LiveSession

pytestmark = [pytest.mark.component, pytest.mark.docker]

WINDOW_S = 0.5  # > FAST_LIVENESS heartbeat (0.2 s): a heartbeat would fit in it


@pytest.mark.asyncio
async def test_reconcile_is_first_with_a_suspension_point_in_the_window(
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    identity: NodeIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime.manager
    assert manager is not None
    received: list[tuple[str, str]] = []
    original = manager.handle_message

    async def recording(live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        received.append((live.session_id, msg.WhichOneof("payload") or "?"))
        await original(live, msg)

    monkeypatch.setattr(manager, "handle_message", recording)

    in_window = asyncio.Event()
    tried_in_window = 0

    async def hook() -> None:
        if client.stats.sessions_accepted > 1:  # reconnects only
            in_window.set()
            await asyncio.sleep(WINDOW_S)
            in_window.clear()

    client = AgentSessionClient(
        make_agent_settings(data_dir, grpc_target), identity, on_online=hook
    )
    task = asyncio.create_task(client.run())

    async def report_forever() -> None:
        nonlocal tried_in_window
        while True:
            if client.state is AgentState.ONLINE:
                if in_window.is_set():
                    tried_in_window += 1
                with contextlib.suppress(Exception):
                    await client.send_report(
                        "resource_update", agent_pb2.ResourceUpdate(cpu_percent=1.0)
                    )
            await asyncio.sleep(0.01)

    reporter: asyncio.Task[Any] = asyncio.create_task(report_forever())
    try:
        await client.wait_for_state(AgentState.ONLINE, timeout_s=10.0)
        await asyncio.sleep(0.1)  # a few reports on the first session
        first = manager.get(str(identity.node_id))
        assert first is not None
        await manager.end_session(first, SessionEndReason.PROTOCOL_ERROR)

        async def reconciled_and_reporting() -> bool:
            second = manager.get(str(identity.node_id))
            if second is None or second is first:
                return False
            mine = [p for s, p in received if s == second.session_id]
            return "reconcile" in mine and mine.count("resource_update") >= 2

        await poll_until(reconciled_and_reporting, timeout_s=10.0)
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
        client.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 10.0)

    assert tried_in_window > 0, "the reporter never ran inside the window"
    second_session = next(s for s, _ in received if s != first.session_id)
    second = [p for s, p in received if s == second_session]
    assert second[0] == "reconcile", (
        f"first message of the reconnect was {second[0]!r}: {second[:10]} - a "
        "report or heartbeat overtook the Reconcile (D21 gate broken)"
    )
