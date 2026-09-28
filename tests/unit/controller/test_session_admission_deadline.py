"""V0.4 G1 (D41): admission has a deadline.

A Hello's admission (lock wait + registry round trips) that misses
``admission_timeout_s`` is refused ``REJECT_CODE_UNAVAILABLE``; its claim is
released, a deferred end is settled **with** its event, the lease timer is woken,
and an admission cut short after it may have written the node row never leaves
that row ONLINE for a session that does not exist.

Injected slow registry: the in-memory fenced registry of ``test_session_linger``
with one method held on an Event (never released - a hung database). The
deadline is 20 ms of real loop time (``asyncio.wait``); everything else runs on
the fake clock and ``LeaseTimer.tick``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
import structlog
from test_session_linger import CFG, NOW0, FencedRegistry, admit, hello, make, settle

from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import RegistryError, SessionEndReason
from meshforge.controller.session.manager import Accepted, Rejected, SessionManager

pytestmark = pytest.mark.unit

RejectCode = agent_pb2.HelloAck.RejectCode
DEADLINE_S = 0.02


class Hang:
    """Replace one registry coroutine: hold every call (or raise) until released."""

    def __init__(self, reg: FencedRegistry, name: str, *, fail: bool = False) -> None:
        self._inner = getattr(reg, name)
        self._name = name
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail
        self.calls = 0
        setattr(reg, name, self)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.held.set()
        if self.fail:
            raise RegistryError(f"{self._name} failed (test)", details={})
        await self.release.wait()
        return await self._inner(*args, **kwargs)


def state_changes(
    logs: Sequence[Mapping[str, Any]], node: str
) -> list[tuple[str, str, str]]:
    return [
        (e["from"], e["to"], e["liveness_event"])
        for e in logs
        if e["event"] == "node.state_changed" and e.get("node_id") == node
    ]


def make_slow() -> tuple[SessionManager, FencedRegistry, Any, Any]:
    return make(admission_timeout_s=DEADLINE_S)


async def drain_chains() -> None:
    for _ in range(5):
        await settle()


# -------------------------------------------------------- nothing written yet


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "settled_state", "lingers"),
    [
        (SessionEndReason.AGENT_CLOSED, NodeState.OFFLINE, False),
        (SessionEndReason.STREAM_LOST, NodeState.SUSPECT, True),
    ],
)
async def test_a_hung_get_node_is_refused_unavailable_and_the_deferred_end_settles(
    reason: SessionEndReason, settled_state: NodeState, lingers: bool
) -> None:
    manager, reg, _clock, _timer = make_slow()
    node = new_node_id()
    old = await admit(manager, node, 1)
    hang = Hang(reg, "get_node")
    with structlog.testing.capture_logs() as logs:
        pending = asyncio.create_task(manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"))
        await asyncio.wait_for(hang.held.wait(), 1.0)
        await manager.end_session(old, reason)
        assert manager._deferred_ends.get(node) is old  # claimed: no event yet
        assert old.state is NodeState.ONLINE
        manager.wake.clear()
        result = await asyncio.wait_for(pending, 1.0)
    assert isinstance(result, Rejected)
    assert result.reject_code == RejectCode.REJECT_CODE_UNAVAILABLE
    assert result.abort_status is None  # a HelloAck{accepted=false}, retryable
    # Claim released, the deferred end settled with its own event, timer woken.
    assert manager._pending_hellos == {}
    assert manager._deferred_ends == {}
    assert old.state is settled_state
    assert old.lingering is lingers
    assert manager.wake.is_set()
    (timeout,) = [e for e in logs if e["event"] == "session.admission_timeout"]
    assert timeout["row_written"] is False and timeout["timeout_s"] == DEADLINE_S
    await drain_chains()
    assert reg.nodes[node].state is settled_state  # never left ONLINE
    assert manager.get(node) is None


@pytest.mark.asyncio
async def test_a_hung_admission_does_not_hold_the_lock_waiters_forever() -> None:
    """Hellos queued behind a hung one are refused too - each within its own
    deadline - and the next Hello after the database recovers is admitted."""
    manager, reg, _clock, _timer = make_slow()
    hang = Hang(reg, "get_node")
    a, b = new_node_id(), new_node_id()
    first = asyncio.create_task(manager.admit(hello(a, 1), "ipv4:1.2.3.4:5"))
    await asyncio.wait_for(hang.held.wait(), 1.0)
    second = await asyncio.wait_for(manager.admit(hello(b, 1), "ipv4:1.2.3.4:6"), 1.0)
    assert isinstance(second, Rejected)
    assert second.reject_code == RejectCode.REJECT_CODE_UNAVAILABLE
    assert isinstance(await asyncio.wait_for(first, 1.0), Rejected)
    hang.release.set()  # the database is back
    await settle()
    assert isinstance(await manager.admit(hello(b, 2), "ipv4:1.2.3.4:6"), Accepted)


@pytest.mark.asyncio
async def test_a_cancelled_admission_releases_its_claim() -> None:
    """The gRPC handler is cancelled (the agent gave up) mid-admission."""
    manager, reg, _clock, _timer = make(admission_timeout_s=5.0)
    node = new_node_id()
    old = await admit(manager, node, 1)
    hang = Hang(reg, "get_node")
    pending = asyncio.create_task(manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"))
    await asyncio.wait_for(hang.held.wait(), 1.0)
    await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert manager._pending_hellos == {}
    assert old.state is NodeState.OFFLINE  # settled with its event
    await drain_chains()
    leaked = [
        t
        for t in asyncio.all_tasks()
        if t.get_name().startswith("session-admit") and not t.done()
    ]
    assert leaked == []


# -------------------------------------------- the node row may already be written


@pytest.mark.asyncio
async def test_a_hang_after_the_row_write_never_leaves_it_online() -> None:
    """``upsert_node_on_hello`` committed (row ONLINE, pointing at the new
    session), then ``open_session`` hung. The attempt becomes a session lost at
    once: SUSPECT now (the agent is alive - it just said Hello), OFFLINE at the
    lease edge unless its retry is admitted first."""
    manager, reg, clock, timer = make_slow()
    node = new_node_id()
    old = await admit(manager, node, 1)
    Hang(reg, "open_session")
    with structlog.testing.capture_logs() as logs:
        result = await asyncio.wait_for(
            manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"), 1.0
        )
        await drain_chains()
    assert isinstance(result, Rejected)
    assert result.reject_code == RejectCode.REJECT_CODE_UNAVAILABLE
    row = reg.nodes[node]
    entry = manager.get_lingering(node)
    assert entry is not None and entry.session_id == row.last_session_id
    assert entry.fence_ids == (old.session_id,)  # superseded by the attempt
    assert row.state is NodeState.SUSPECT  # the row was ONLINE; now honest
    assert old.end_reason is SessionEndReason.SUPERSEDED
    (abandoned,) = [e for e in logs if e["event"] == "session.admission_abandoned"]
    assert abandoned["also_fenced_on"] == [old.session_id]
    # No reconnect: OFFLINE exactly at the lease edge, measured from the Hello.
    clock.now = NOW0 + CFG.offline_after_s - 1e-3
    await timer.tick(clock.now)
    assert reg.nodes[node].state is NodeState.SUSPECT
    clock.now = NOW0 + CFG.offline_after_s
    await timer.tick(clock.now)
    await drain_chains()
    assert reg.nodes[node].state is NodeState.OFFLINE
    assert manager.get_lingering(node) is None


@pytest.mark.asyncio
async def test_the_retry_after_an_abandoned_admission_is_online_with_no_offline() -> (
    None
):
    manager, reg, _clock, _timer = make_slow()
    node = new_node_id()
    await admit(manager, node, 1)
    hang = Hang(reg, "open_session")
    assert isinstance(await manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"), Rejected)
    await drain_chains()
    hang.release.set()
    del reg.open_session  # the database is back: the real method again
    with structlog.testing.capture_logs() as logs:
        retry = await manager.admit(hello(node, 3), "ipv4:1.2.3.4:5")
        await drain_chains()
    assert isinstance(retry, Accepted)
    assert state_changes(logs, node) == [("suspect", "online", "reconnected")]
    assert reg.nodes[node].state is NodeState.ONLINE
    assert manager.get_lingering(node) is None


@pytest.mark.asyncio
async def test_a_registry_error_after_the_row_write_never_leaves_it_online() -> None:
    """Same hazard without a timeout: ``open_session`` raises after the upsert
    committed. The error still reaches the servicer (INTERNAL), and the row is
    not left ONLINE."""
    manager, reg, _clock, _timer = make(admission_timeout_s=5.0)
    node = new_node_id()
    Hang(reg, "open_session", fail=True)
    with pytest.raises(RegistryError):
        await manager.admit(hello(node, 1), "ipv4:1.2.3.4:5")
    await drain_chains()
    assert reg.nodes[node].state is NodeState.SUSPECT
    assert manager.get_lingering(node) is not None


@pytest.mark.asyncio
async def test_an_abandoned_admission_takes_over_a_linger() -> None:
    """The lingering entry may still own the row (the upsert may not have
    committed): the new entry's writes are fenced on it too."""
    manager, reg, _clock, _timer = make_slow()
    node = new_node_id()
    first = await admit(manager, node, 1)
    await manager.end_session(first, SessionEndReason.STREAM_LOST)  # lingers
    Hang(reg, "open_session")
    assert isinstance(await manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"), Rejected)
    await drain_chains()
    entry = manager.get_lingering(node)
    assert entry is not None and entry.fence_ids == (first.session_id,)
    assert not first.lingering  # replaced, its chain finished
    assert first.writer.done()
    assert reg.nodes[node].state is NodeState.SUSPECT


@pytest.mark.asyncio
async def test_an_abandoned_admission_takes_over_a_deferred_end_without_its_event() -> (
    None
):
    """The old session ended (Goodbye) while the Hello was pending, the row was
    then written, then the admission hung. The deferred end is taken over with
    no event of its own - the agent is alive (it just said Hello), so SUSPECT,
    not OFFLINE - and its id is fenced on too."""
    manager, reg, _clock, _timer = make_slow()
    node = new_node_id()
    old = await admit(manager, node, 1)
    get_hang = Hang(reg, "get_node")
    Hang(reg, "open_session")
    with structlog.testing.capture_logs() as logs:
        pending = asyncio.create_task(manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"))
        await asyncio.wait_for(get_hang.held.wait(), 1.0)
        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)  # deferred
        get_hang.release.set()
        assert isinstance(await asyncio.wait_for(pending, 1.0), Rejected)
        await drain_chains()
    entry = manager.get_lingering(node)
    assert entry is not None and entry.fence_ids == (old.session_id,)
    assert manager._deferred_ends == {}
    assert old.end_reason is SessionEndReason.AGENT_CLOSED  # its honest reason
    assert old.state is NodeState.ONLINE  # no event of its own
    assert state_changes(logs, node) == [("online", "suspect", "stream_lost")]
    assert reg.nodes[node].state is NodeState.SUSPECT


def test_the_admission_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="admission_timeout_s"):
        make(admission_timeout_s=0.0)
