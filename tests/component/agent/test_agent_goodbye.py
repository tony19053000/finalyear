"""V0.4 G1 (D40), real agent + real controller + real PostgreSQL over real gRPC:
a requested stop ends the stream with ``Goodbye``, and the controller turns that
into ``agent_closed`` + OFFLINE on receipt - no linger, no SUSPECT, no lease.

(The SIGTERM'd agent *process* is ``test_agent_process.py``; the raw-client "old
agent without Goodbye" and cancel/channel-close cases are
``tests/component/controller/test_stream_end_liveness.py``.)
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog
from mf_agent_harness import AgentFactory
from mf_session_client import poll_until, wait_session_end_reason

from meshforge.agent.session.state import AgentState
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.component


def transitions(logs: list[dict[str, Any]], node_id: str) -> list[tuple[str, str, str]]:
    return [
        (e["from"], e["to"], e["liveness_event"])
        for e in logs
        if e["event"] == "node.state_changed" and e.get("node_id") == node_id
    ]


@pytest.mark.asyncio
async def test_a_real_agents_requested_stop_is_offline_now_and_agent_closed(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    agents: AgentFactory,
) -> None:
    agent = await agents(grpc_target)
    await agent.client.wait_for_state(AgentState.ONLINE, timeout_s=10.0)
    node_id = agent.client.node_id
    session_id = agent.client.session_id
    assert runtime.manager is not None
    lease_s = runtime.manager.liveness.offline_after_s

    with structlog.testing.capture_logs() as logs:
        assert await agent.stop() is None  # run() returned normally

        async def offline() -> bool:
            rec = await registry.get_node(node_id)
            return rec is not None and rec.state is NodeState.OFFLINE

        await poll_until(offline, timeout_s=lease_s, interval_s=0.01)

    # OFFLINE is committed before close_session (D19 chain order): wait for it.
    reason = await wait_session_end_reason(registry, session_id)
    assert reason is SessionEndReason.AGENT_CLOSED
    # Straight ONLINE -> OFFLINE by the Goodbye: never through SUSPECT, never
    # by the lease, and nothing left lingering.
    assert transitions(logs, node_id) == [("online", "offline", "stream_closed")]
    assert not [e for e in logs if e["event"] == "node.lease_expired"]
    assert runtime.manager.get(node_id) is None
    assert runtime.manager.get_lingering(node_id) is None
    (bye,) = [e for e in logs if e["event"] == "session.goodbye"]
    assert bye["session_id"] == session_id and bye["reason"] == "REASON_SHUTDOWN"
    sent = [e for e in logs if e["event"] == "agent.session.goodbye_sent"]
    assert [e["session_id"] for e in sent] == [session_id]
    # The agent saw the controller's final status OK, not an abort.
    (end,) = agent.client.stats.stream_ends
    assert end.code is not None and end.code.name == "OK"
