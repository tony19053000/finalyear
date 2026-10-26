"""V0.17 G5 tester (brief §5e item 4, D393/Q37): agent <-> controller version
compatibility against a REAL controller (PostgreSQL) and the REAL agent client.

Acceptance: the previous minor is accepted (and flagged "behind"); older, newer
and unreadable versions are refused with ``version_unsupported``; the refused
agent stops after ONE cycle and never loops; a refusal stores nothing - not
even over the record of a node that was accepted earlier; a refused agent does
not disturb an accepted one on the same controller.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mf_agent_harness import AgentFactory, make_controller_settings

from meshforge import __version__
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.errors import VersionUnsupportedError
from meshforge.agent.session.state import AgentState
from meshforge.common.versioning import VERSION_UNSUPPORTED, parse_minor
from meshforge.controller.api.shape import version_status
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

OWN = parse_minor(__version__)
assert OWN is not None
PREVIOUS = (
    f"{OWN.major}.{OWN.minor - 1}.5"
    if OWN.minor > 0
    else f"{max(OWN.major - 1, 0)}.0.5"
)


async def _live(rt: ControllerRuntime, node_id: str, within_s: float = 10.0) -> object:
    manager = rt.manager
    assert manager is not None
    for _ in range(int(within_s * 10)):
        live = manager.get(node_id)
        if live is not None:
            return live
        await asyncio.sleep(0.1)
    raise AssertionError(f"{node_id} never connected")


@pytest.mark.asyncio
async def test_the_previous_minor_is_accepted_and_flagged_behind(
    pg_settings: DatabaseSettings, agents: AgentFactory, identity: NodeIdentity
) -> None:
    if OWN.major == 0 and OWN.minor == 0:
        pytest.skip("no previous minor of 0.0")
    async with ControllerRuntime.start(make_controller_settings(pg_settings)) as rt:
        running = await agents(
            f"127.0.0.1:{rt.grpc_port}", identity=identity, agent_version=PREVIOUS
        )
        live = await _live(rt, identity.node_id)
        assert live.agent_version  # type: ignore[attr-defined] == PREVIOUS
        assert version_status(PREVIOUS) == "behind"
        assert running.client.state is not AgentState.STOPPED
        await running.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_version",
    ["banana", "0", "  ", "v", "x.y.z"],
)
async def test_an_unreadable_version_is_refused_once_and_stops(
    pg_settings: DatabaseSettings,
    agents: AgentFactory,
    identity: NodeIdentity,
    agent_version: str,
) -> None:
    async with ControllerRuntime.start(make_controller_settings(pg_settings)) as rt:
        running = await agents(
            f"127.0.0.1:{rt.grpc_port}", identity=identity, agent_version=agent_version
        )
        exc = await running.outcome(timeout_s=10.0)
        assert rt.registry is not None
        assert await rt.registry.get_node(identity.node_id) is None
    assert isinstance(exc, VersionUnsupportedError), exc
    assert exc.message.startswith(f"{VERSION_UNSUPPORTED}: ")
    assert running.client.stats.cycles == 1


@pytest.mark.asyncio
async def test_a_refused_agent_does_not_retry_even_after_the_backoff_has_passed(
    pg_settings: DatabaseSettings, agents: AgentFactory, identity: NodeIdentity
) -> None:
    """No reconnect loop: the harness backoff is a fraction of a second, so
    waiting several of them would show a second cycle if there were a loop."""
    async with ControllerRuntime.start(make_controller_settings(pg_settings)) as rt:
        running = await agents(
            f"127.0.0.1:{rt.grpc_port}",
            identity=identity,
            agent_version=f"{OWN.major}.{OWN.minor + 1}.0",
        )
        exc = await running.outcome(timeout_s=10.0)
        assert isinstance(exc, VersionUnsupportedError)
        await asyncio.sleep(3.0)
        assert running.client.stats.cycles == 1
        assert running.client.state is AgentState.STOPPED
        manager = rt.manager
        assert manager is not None and len(manager) == 0


@pytest.mark.asyncio
async def test_a_refusal_never_overwrites_what_an_earlier_good_connection_stored(
    pg_settings: DatabaseSettings, agents: AgentFactory, identity: NodeIdentity
) -> None:
    async with ControllerRuntime.start(make_controller_settings(pg_settings)) as rt:
        good = await agents(
            f"127.0.0.1:{rt.grpc_port}",
            identity=identity,
            agent_version=f"{OWN.major}.{OWN.minor}.1",
        )
        await _live(rt, identity.node_id)
        await good.stop()
        assert rt.registry is not None
        before = await rt.registry.get_node(identity.node_id)
        for _ in range(100):  # the stop is seen by the controller a moment later
            before = await rt.registry.get_node(identity.node_id)
            assert before is not None
            if before.state.value == "offline":
                break
            await asyncio.sleep(0.1)
        # the same node comes back as an agent the controller refuses
        bad = await agents(
            f"127.0.0.1:{rt.grpc_port}",
            identity=identity,
            agent_version=f"{OWN.major}.{OWN.minor + 3}.0",
        )
        exc = await bad.outcome(timeout_s=10.0)
        assert isinstance(exc, VersionUnsupportedError)
        after = await rt.registry.get_node(identity.node_id)
        assert after is not None
        assert after == before  # not even a touched timestamp or version


@pytest.mark.asyncio
async def test_refused_agents_do_not_disturb_an_accepted_one(
    pg_settings: DatabaseSettings,
    agents: AgentFactory,
    identity: NodeIdentity,
    tmp_path: Path,
) -> None:
    # floor raised to this minor so "too old" exists (0.1 has no older minor
    # than the accepted previous one)
    settings = make_controller_settings(pg_settings, min_agent_version=str(OWN))
    async with ControllerRuntime.start(settings) as rt:
        address = f"127.0.0.1:{rt.grpc_port}"
        good = await agents(address, identity=identity)
        await _live(rt, identity.node_id)
        cycles_before = good.client.stats.cycles
        losers = [
            await agents(
                address,
                directory=tmp_path / f"other{n}",
                agent_version=("0.0.1", "99.0.0", "banana", "0.0.9")[n],
            )
            for n in range(4)
        ]
        outcomes = [await loser.outcome(timeout_s=10.0) for loser in losers]
        assert all(isinstance(o, VersionUnsupportedError) for o in outcomes), outcomes
        assert [loser.client.stats.cycles for loser in losers] == [1, 1, 1, 1]
        manager = rt.manager
        assert manager is not None
        assert len(manager) == 1 and manager.get(identity.node_id) is not None
        assert good.client.stats.cycles == cycles_before  # never reconnected
        await good.stop()
