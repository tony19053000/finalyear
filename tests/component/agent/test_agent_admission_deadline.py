"""V0.4 G1 (D41), real agent + real controller + real PostgreSQL: while the
controller's registry hangs, the agent's Hello is refused
``REJECT_CODE_UNAVAILABLE``; the agent backs off (never fatal, never a tight
loop) and is admitted once the database answers again.

The hang is injected on the running controller's own ``Registry`` instance
(``get_node`` for this agent's node held forever while ``holding``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from mf_agent_harness import AgentFactory
from mf_session_client import poll_until

from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.state import AgentState
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.component


class HungGetNode:
    def __init__(self, registry: Registry, node_id: str) -> None:
        self._inner = registry.get_node
        self._registry = registry
        self._node_id = node_id
        self.holding = True
        self.calls = 0
        registry.get_node = self  # type: ignore[method-assign]

    async def __call__(self, node_id: str) -> Any:
        if node_id == self._node_id and self.holding:
            self.calls += 1
            await asyncio.Event().wait()  # a database that never answers
        return await self._inner(node_id)

    def restore(self) -> None:
        del self._registry.get_node


@pytest.mark.asyncio
async def test_the_agent_backs_off_on_unavailable_and_later_succeeds(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    identity: NodeIdentity,
    agents: AgentFactory,
) -> None:
    assert runtime.manager is not None
    node_id = str(identity.node_id)
    hang = HungGetNode(runtime.manager._registry, node_id)
    try:
        with structlog.testing.capture_logs() as logs:
            agent = await agents(grpc_target, identity=identity)

            async def refused_twice() -> bool:
                return (
                    len(
                        [
                            e
                            for e in logs
                            if e["event"] == "agent.session.hello_rejected"
                            and e["reject_code"] == "REJECT_CODE_UNAVAILABLE"
                        ]
                    )
                    >= 2
                )

            await poll_until(refused_twice, timeout_s=15.0, interval_s=0.02)
            assert agent.client.state is not AgentState.ONLINE
            assert not agent.task.done()  # never fatal
            hang.holding = False  # the database answers again
            await agent.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
        rejected = [e for e in logs if e["event"] == "agent.session.hello_rejected"]
        assert rejected and all(e["action"] == "backoff" for e in rejected)
        backoffs = [e for e in logs if e["event"] == "agent.session.backoff"]
        assert len(backoffs) >= len(rejected)  # a sleep after every refusal

        async def online() -> bool:
            rec = await registry.get_node(node_id)
            return rec is not None and rec.state is NodeState.ONLINE

        await poll_until(online, timeout_s=5.0, interval_s=0.01)
        assert await agent.stop() is None
    finally:
        hang.restore()
