"""V0.3 G6 fix round, D28: a Hello that arrives before its node's previous session's
end is decided owns the node's state.

The previous session then ends with **no** state event (like a supersession);
admission never derives ``prior`` from a database row it read before a close that
happened during its own await. A stale-epoch Hello claims nothing, and an admission
that fails settles the deferred end with the event it would have fired.

The seam: the in-memory fenced registry of ``test_session_linger`` with its
``get_node`` held on an Event, so a Hello's admission can be parked exactly where a
slow database round trip would park it. Fake clock, no sleep.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
import structlog
from test_session_linger import (
    CFG,
    FakeClock,
    FencedRegistry,
    admit,
    hello,
    make,
    settle,
)

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import NodeRecord, RegistryError, SessionEndReason
from meshforge.controller.session.manager import (
    Accepted,
    LiveSession,
    Rejected,
    SessionManager,
)

pytestmark = pytest.mark.unit


class GatedGet:
    """Wraps a FencedRegistry's ``get_node``: hold it on ``release``, or raise."""

    def __init__(self, reg: FencedRegistry) -> None:
        self._inner = reg.get_node
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False
        reg.get_node = self  # type: ignore[method-assign, unused-ignore]

    async def __call__(self, node_id: str) -> NodeRecord | None:
        self.held.set()
        await self.release.wait()
        if self.fail:
            raise RegistryError(
                "database unreachable (test)", details={"operation": "get_node"}
            )
        return cast("NodeRecord | None", await self._inner(node_id))


def state_of(live: LiveSession) -> NodeState:
    """Read the state through a call, so a type checker does not keep an earlier
    ``assert live.state is ...`` narrowing across the awaits that change it."""
    return live.state


def state_changes(
    logs: Sequence[Mapping[str, Any]], node: str
) -> list[tuple[str, str]]:
    return [
        (e["from"], e["to"])
        for e in logs
        if e["event"] == "node.state_changed" and e.get("node_id") == node
    ]


async def park_admission(
    manager: SessionManager, gate: GatedGet, node: str, epoch: int
) -> asyncio.Task[Any]:
    task = asyncio.create_task(manager.admit(hello(node, epoch), "ipv4:127.0.0.1:2"))
    await asyncio.wait_for(gate.held.wait(), 1.0)
    return task


@pytest.mark.asyncio
async def test_clean_close_while_a_newer_hello_is_pending_writes_no_state() -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    gate = GatedGet(reg)
    with structlog.testing.capture_logs() as logs:
        task = await park_admission(manager, gate, node, 2)
        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
        # Unregistered at once (the handler is unblocked), but no event yet.
        assert manager.get(node) is None
        assert old.state is NodeState.ONLINE
        gate.release.set()
        result = await task
    assert isinstance(result, Accepted)
    await settle()
    assert state_changes(logs, node) == []  # no online -> offline -> online dip
    assert result.live.state is NodeState.ONLINE
    # The old stream's honest reason is kept; its chain ended with close_session
    # and never carried a state write.
    assert old.end_reason is SessionEndReason.AGENT_CLOSED
    assert [w for w in reg.writes if w[1] == old.session_id and w[0] != "upsert"] == [
        ("close_session", old.session_id, SessionEndReason.AGENT_CLOSED, True)
    ]
    assert old.writer.done()
    closed = [e for e in logs if e["event"] == "session.closed"]
    assert closed[-1]["owned_by_hello"] is True
    assert reg.nodes[node].state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_abrupt_end_while_a_newer_hello_is_pending_does_not_linger() -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    gate = GatedGet(reg)
    with structlog.testing.capture_logs() as logs:
        task = await park_admission(manager, gate, node, 2)
        manager.end_session_nowait(old, SessionEndReason.STREAM_LOST)
        gate.release.set()
        assert isinstance(await task, Accepted)
    assert state_changes(logs, node) == []
    assert manager.get_lingering(node) is None and not old.lingering


@pytest.mark.asyncio
async def test_a_stale_epoch_hello_does_not_suppress_a_genuine_clean_close() -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 5)
    gate = GatedGet(reg)
    with structlog.testing.capture_logs() as logs:
        task = await park_admission(manager, gate, node, 3)  # stale: 3 <= 5
        assert not manager.admission_pending(old)
        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
        # Decided now, not after the stale admission returns.
        assert old.state is NodeState.OFFLINE
        assert state_changes(logs, node) == [("online", "offline")]
        gate.release.set()
        result = await task
    assert isinstance(result, Rejected)
    await settle()
    assert reg.nodes[node].state is NodeState.OFFLINE


@pytest.mark.asyncio
async def test_an_equal_epoch_hello_claims_nothing_either() -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 4)
    gate = GatedGet(reg)
    task = await park_admission(manager, gate, node, 4)
    assert not manager.admission_pending(old)
    gate.release.set()
    assert isinstance(await task, Rejected)


@pytest.mark.parametrize("failure", ["registry_error", "stale_vs_record"])
@pytest.mark.asyncio
async def test_a_failed_admission_settles_the_deferred_end_with_its_event(
    failure: str,
) -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    gate = GatedGet(reg)
    task = await park_admission(manager, gate, node, 2)
    await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
    assert old.state is NodeState.ONLINE  # deferred
    if failure == "registry_error":
        gate.fail = True
    else:
        # Another process already persisted a higher epoch: this Hello is stale
        # after all, and must not own anything.
        rec = reg.nodes[node]
        reg.nodes[node] = dataclasses.replace(rec, session_epoch=9)
    gate.release.set()
    if failure == "registry_error":
        with pytest.raises(RegistryError):
            await task
    else:
        assert isinstance(await task, Rejected)
    await settle()
    # As if no Hello had been pending: the clean close is OFFLINE, then the END.
    assert state_of(old) is NodeState.OFFLINE
    assert [w for w in reg.writes if w[1] == old.session_id and w[0] != "upsert"] == [
        ("set_node_state", old.session_id, NodeState.OFFLINE, True),
        ("close_session", old.session_id, SessionEndReason.AGENT_CLOSED, True),
    ]
    assert old.writer.done()


@pytest.mark.asyncio
async def test_the_lease_timer_decides_nothing_for_a_claimed_node() -> None:
    clock = FakeClock()
    manager, reg, clock, timer = make(clock)
    node = new_node_id()
    old = await admit(manager, node, 1)
    manager.end_session_nowait(old, SessionEndReason.STREAM_LOST)  # lingering
    assert manager.get_lingering(node) is old
    gate = GatedGet(reg)
    task = await park_admission(manager, gate, node, 2)
    clock.now += CFG.offline_after_s + 1.0  # past the lingering lease edge
    # Skipped, and it offers no (past) deadline, so the timer cannot spin.
    assert await timer.tick(clock.now) is None
    assert old.state is NodeState.SUSPECT and old.lingering
    gate.release.set()
    result = await task
    assert isinstance(result, Accepted)
    assert result.live.state is NodeState.ONLINE
    assert not old.lingering
    await settle()
    assert NodeState.OFFLINE not in [v for _s, v, _ok in reg.states_written(node)]


@pytest.mark.asyncio
async def test_after_a_failed_claim_the_timer_expires_the_linger_as_usual() -> None:
    clock = FakeClock()
    manager, reg, clock, timer = make(clock)
    node = new_node_id()
    old = await admit(manager, node, 1)
    manager.end_session_nowait(old, SessionEndReason.STREAM_LOST)
    gate = GatedGet(reg)
    gate.fail = True
    task = await park_admission(manager, gate, node, 2)
    clock.now += CFG.offline_after_s + 1.0
    await timer.tick(clock.now)
    assert old.lingering
    manager.wake.clear()
    gate.release.set()
    with pytest.raises(RegistryError):
        await task
    assert manager.wake.is_set()  # the timer is told to look again
    await timer.tick(clock.now)
    assert old.state is NodeState.OFFLINE and not old.lingering


@pytest.mark.asyncio
async def test_prior_comes_from_the_deferred_sessions_memory_not_the_row() -> None:
    """The row lags memory (a SUSPECT write still queued on the old chain): the
    Hello must be SUSPECT -> RECONNECTED -> ONLINE, as for a live or lingering
    predecessor. Deriving ``prior`` from the row would log nothing, leaving the
    log's last word on this node ``online -> suspect``."""
    from meshforge.controller.liveness import LivenessEvent

    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    reg.gate.clear()  # the old chain's state write stays queued
    await manager.apply_event(old, LivenessEvent.LEASE_MISSED, silence_s=2.5)
    await settle()
    assert reg.nodes[node].state is NodeState.ONLINE  # row behind memory
    gate = GatedGet(reg)
    with structlog.testing.capture_logs() as logs:
        task = await park_admission(manager, gate, node, 2)
        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
        gate.release.set()
        result = await task
    assert isinstance(result, Accepted)
    assert state_changes(logs, node) == [("suspect", "online")]
    reg.gate.set()
    await settle()
    assert reg.nodes[node].state is NodeState.ONLINE  # the old write was fenced
