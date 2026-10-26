"""V0.17 G5 (D393, Q37): a real controller refuses a too-old and a too-new
agent with ``version_unsupported``; the real agent stops on the first reply
(one cycle, no retry loop) with a typed error naming both versions and the fix,
and the controller never registers the node. An agent of the same minor still
connects.

The controller is this checkout (``meshforge.__version__``); the agents claim
other versions through ``[agent] agent_version`` (what Hello carries).
"""

from __future__ import annotations

import asyncio

import pytest
from mf_agent_harness import AgentFactory, make_controller_settings

from meshforge import __version__
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.errors import VersionUnsupportedError
from meshforge.agent.session.state import AgentState
from meshforge.common.errors import ExitCode
from meshforge.common.versioning import VERSION_UNSUPPORTED, parse_minor
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

OWN = parse_minor(__version__)
assert OWN is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_version", "floor", "fix"),
    [
        (f"{OWN.major}.{OWN.minor + 1}.0", "", "upgrade the controller first"),
        (f"{OWN.major + 1}.0.0", "", "upgrade the controller first"),
        ("0.0.1", str(OWN), "upgrade the MeshForge agent"),
    ],
    ids=["newer-minor", "newer-major", "older-than-floor"],
)
async def test_unsupported_agent_stops_once_with_the_reason(
    pg_settings: DatabaseSettings,
    agents: AgentFactory,
    identity: NodeIdentity,
    agent_version: str,
    floor: str,
    fix: str,
) -> None:
    settings = make_controller_settings(pg_settings, min_agent_version=floor)
    async with ControllerRuntime.start(settings) as rt:
        running = await agents(
            f"127.0.0.1:{rt.grpc_port}", identity=identity, agent_version=agent_version
        )
        exc = await running.outcome(timeout_s=10.0)
        registry = rt.registry
        assert registry is not None
        node = await registry.get_node(identity.node_id)
    assert isinstance(exc, VersionUnsupportedError), exc
    assert exc.exit_code == ExitCode.CONFIG_ERROR
    assert exc.message.startswith(f"{VERSION_UNSUPPORTED}: ")
    assert agent_version in exc.message and __version__ in exc.message
    assert fix in exc.message
    assert running.client.state is AgentState.STOPPED
    assert running.client.stats.cycles == 1  # no retry loop
    assert node is None  # refused before anything was stored


@pytest.mark.asyncio
async def test_same_minor_agent_connects(
    pg_settings: DatabaseSettings, agents: AgentFactory, identity: NodeIdentity
) -> None:
    settings = make_controller_settings(pg_settings, min_agent_version=str(OWN))
    async with ControllerRuntime.start(settings) as rt:
        running = await agents(
            f"127.0.0.1:{rt.grpc_port}",
            identity=identity,
            agent_version=f"{OWN.major}.{OWN.minor}.99",
        )
        manager = rt.manager
        assert manager is not None
        for _ in range(100):
            if manager.get(identity.node_id) is not None:
                break
            await asyncio.sleep(0.1)
        live = manager.get(identity.node_id)
        assert live is not None
        assert live.agent_version == f"{OWN.major}.{OWN.minor}.99"
        await running.stop()
