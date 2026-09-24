"""V0.3 G6 (D22) over real gRPC: a Hello with a non-printable string is refused.

Controller side: the stream ends ``INVALID_ARGUMENT`` naming the field and the code
point (never the value) and nothing is persisted. Agent side: the real
:class:`AgentSessionClient` surfaces that as a typed
:class:`~meshforge.agent.session.errors.AgentContractError` (exit
``CONFIG_ERROR``) after **one** attempt - not a reconnect loop.
"""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest
from mf_agent_harness import make_agent_settings
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.errors import AgentContractError
from meshforge.agent.session.state import AgentState
from meshforge.common.errors import ExitCode
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker]

ESCAPE_TITLE = "lap\x1b]0;pwned\x07top"  # an OSC "set window title" sequence


@pytest.mark.asyncio
async def test_controller_refuses_a_non_printable_hello_and_persists_nothing(
    runtime: ControllerRuntime, grpc_target: str, registry: Registry
) -> None:
    async with grpc.aio.insecure_channel(grpc_target) as ch:
        agent = FakeAgent(ch)
        reply = await agent.hello(
            hardware=agent_pb2.HardwareSummary(
                gpus=[agent_pb2.HardwareSummary.Gpu(model="RTX\x9b2J")]
            )
        )
        assert reply is None
        assert agent.end_code is grpc.StatusCode.INVALID_ARGUMENT
        assert "Hello.hardware.gpus[0].model" in agent.end_details
        assert "U+009B" in agent.end_details
        assert "RTX" not in agent.end_details
        assert await registry.get_node(agent.node_id) is None
        assert await registry.list_sessions(agent.node_id) == []
        await agent.close()


@pytest.mark.asyncio
async def test_the_real_agent_gets_a_typed_error_not_a_reconnect_loop(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    data_dir: Path,
    identity: NodeIdentity,
) -> None:
    settings = make_agent_settings(data_dir, grpc_target)
    client = AgentSessionClient(
        settings,
        identity,
        host_facts=(ESCAPE_TITLE, "TestOS 1.0", "x86_64"),
    )
    with pytest.raises(AgentContractError) as info:
        await client.run()
    err = info.value
    assert err.exit_code is ExitCode.CONFIG_ERROR
    assert "INVALID_ARGUMENT" in str(err)
    assert "Hello.hostname" in str(err) and "U+001B" in str(err)
    assert "pwned" not in str(err)
    assert client.stats.cycles == 1  # one attempt, then a typed stop
    assert client.stats.sessions_accepted == 0
    assert client.state is AgentState.STOPPED
    assert await registry.get_node(str(identity.node_id)) is None
