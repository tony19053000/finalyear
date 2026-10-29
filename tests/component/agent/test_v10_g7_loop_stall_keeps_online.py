"""V1.0 G7 (D424): a stall of the controller's own event loop is not agent silence.

The controller and the agent run in one process here (as in
``test_benchmarks_heartbeat``), on fast liveness (heartbeat 0.2 s, OFFLINE after
0.6 s). A synchronous ``time.sleep`` longer than the whole lease blocks the loop
both run on - the same thing a GC pause, a VM steal or a CPU-starved CI runner does
to a real controller. Before D424 the lease timer woke up after the stall, saw
1.3 s of "silence" and expired the session before the loop had read the heartbeats
that were already waiting (CI run 40733787742, ``[nice10]``: ``agent:BACKOFF``).
The timer now notices that it woke up late, listens for one heartbeat interval,
and only then judges the lease: the node stays ONLINE and the stream never drops.

The other half - a node that really goes silent is still expired - is the
existing liveness suite (``test_agent_session``) plus the unit tests of
:class:`~meshforge.controller.session.leases.LeaseTimer`.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from mf_agent_harness import AgentFactory

from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.state import AgentState
from meshforge.controller.liveness import NodeState
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.timeout(120)]

ONLINE_TIMEOUT_S = 15.0
STALL_S = 1.3  # > offline_after_s (0.6 s) of the fast-liveness harness


@pytest.mark.asyncio
async def test_a_loop_stall_longer_than_the_lease_does_not_expire_a_live_node(
    runtime: ControllerRuntime,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
) -> None:
    running = await agents(grpc_target, identity=identity)
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    assert runtime.manager is not None
    node_id = str(identity.node_id)
    offline_after = runtime.manager.liveness.offline_after_s
    assert 2 * offline_after < STALL_S  # the stall really outlasts the lease

    leaves: list[str] = []
    for _ in range(3):  # three separate stalls, each with recovery between
        await asyncio.sleep(1.0)  # several heartbeats: a settled lease
        time.sleep(STALL_S)  # noqa: ASYNC251 - blocking the loop IS the test
        deadline = asyncio.get_running_loop().time() + 1.5
        while asyncio.get_running_loop().time() < deadline:
            live = runtime.manager.get(node_id)
            if live is None or live.state is not NodeState.ONLINE:
                leaves.append(f"controller:{None if live is None else live.state}")
            if running.client.state is not AgentState.ONLINE:
                leaves.append(f"agent:{running.client.state}")
            await asyncio.sleep(0.02)

    assert leaves == [], f"a loop stall expired a live node: {leaves[:5]}"
    assert running.client.stats.stream_ends == []
