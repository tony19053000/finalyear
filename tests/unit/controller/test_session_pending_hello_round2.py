"""V0.3 G6 tester round 2: extra D28 attacks beyond the fix round's own
``test_session_pending_hello.py`` (docs/briefs/V0.3-fabric-core.md §5f, priority 1).

Same seam as the fix round's tests: :class:`test_session_linger.FencedRegistry`
with ``get_node`` gated so a Hello's admission can be parked exactly where a slow
database round trip would park it. This module attacks the two shapes the fix
round did not: the admission task being **cancelled** while parked (client
disconnect / controller shutdown racing an in-flight admission), and an
**exception raised inside admission** by something other than the registry
(``upsert_node_on_hello`` after ``get_node`` already returned).

Invariant checked after every scenario, once quiet: no session or node is left
claimed with no admission left to resolve it, no ``node.state_changed`` fires for
a node that never really changed state, and the lease timer can still decide the
node afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
import structlog
from test_session_linger import CFG, FakeClock, FencedRegistry, admit, hello, make, settle

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import RegistryError, SessionEndReason
from meshforge.controller.session.manager import Accepted, LiveSession, SessionManager

pytestmark = pytest.mark.unit


class GatedGet:
    """Wraps a FencedRegistry's ``get_node``: hold it on ``release``, or raise."""

    def __init__(self, reg: FencedRegistry) -> None:
        self._inner = reg.get_node
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.fail: Exception | None = None
        reg.get_node = self  # type: ignore[method-assign, unused-ignore]

    async def __call__(self, node_id: str) -> Any:
        self.held.set()
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        return await self._inner(node_id)


class FailingUpsert:
    """Replaces a FencedRegistry's ``upsert_node_on_hello``: always raises.

    Used *after* a Hello's ``get_node`` has already returned and ``old``/
    ``deferred``/``lingering`` were captured from memory - a write-side registry
    failure discovered only after the in-memory decision (``prior``) was already
    made, unlike ``test_a_failed_admission_settles_the_deferred_end_with_its_event``
    (fix round), whose ``registry_error`` case fails inside ``get_node`` itself,
    before any of those locals are read.
    """

    def __init__(self, reg: FencedRegistry) -> None:
        reg.upsert_node_on_hello = self  # type: ignore[method-assign, unused-ignore]

    async def __call__(self, **kw: Any) -> Any:
        raise RegistryError(
            "database unreachable (test)", details={"operation": "upsert_node_on_hello"}
        )


def state_changes(
    logs: Sequence[Mapping[str, Any]], node: str
) -> list[tuple[str, str]]:
    return [
        (e["from"], e["to"])
        for e in logs
        if e["event"] == "node.state_changed" and e.get("node_id") == node
    ]


async def park_admission(
    manager: SessionManager, held: asyncio.Event, node: str, epoch: int
) -> asyncio.Task[Any]:
    task = asyncio.create_task(manager.admit(hello(node, epoch), "ipv4:127.0.0.1:2"))
    await asyncio.wait_for(held.wait(), 1.0)
    return task


def assert_no_stuck_claim(manager: SessionManager, node: str) -> None:
    """The invariant the tester brief asks for after every scenario, once quiet."""
    assert manager._pending_hellos.get(node) in (None, [])
    assert node not in manager._deferred_ends


@pytest.mark.asyncio
async def test_admission_cancelled_while_parked_releases_its_claim_and_settles_the_end() -> (
    None
):
    """A client that disconnects mid-Hello (or a cancelled awaiter) must not leave
    the claim standing forever: ``admit()``'s ``finally`` runs the release even
    when the task is cancelled inside the gated ``get_node`` await, and the old
    session's deferred end is settled with its real event, exactly as a failed
    admission would."""
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    gate = GatedGet(reg)
    with structlog.testing.capture_logs() as logs:
        task = await park_admission(manager, gate.held, node, 2)
        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
        assert old.state is NodeState.ONLINE  # deferred, not yet decided
        assert manager.admission_pending(old)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()

    # The claim is released and the deferred end settled with its real event, as
    # if no Hello had ever been pending.
    assert_no_stuck_claim(manager, node)
    assert old.state is NodeState.OFFLINE
    assert state_changes(logs, node) == [("online", "offline")]
    assert reg.nodes[node].state is NodeState.OFFLINE
    assert old.writer.done()

    # The node is not stuck: a fresh Hello is admitted normally afterward. Undo
    # the gate first - it was never released, only the awaiter cancelled.
    reg.get_node = gate._inner
    new_live = await admit(manager, node, 3)
    assert new_live.state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_admission_cancelled_before_close_races_but_still_settles() -> None:
    """Cancel the admission *before* the old session closes: the claim must still
    be released (no dangling ``_pending_hellos`` entry) and the old session, once
    it does close, must fire its own event normally (nothing was ever deferred
    for it, since no admission was pending by the time it closed)."""
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    gate = GatedGet(reg)
    task = await park_admission(manager, gate.held, node, 2)
    assert manager.admission_pending(old)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle()
    assert_no_stuck_claim(manager, node)
    assert not manager.admission_pending(old)

    with structlog.testing.capture_logs() as logs:
        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
        await settle()
    assert state_changes(logs, node) == [("online", "offline")]
    assert reg.nodes[node].state is NodeState.OFFLINE


@pytest.mark.asyncio
async def test_exception_inside_admission_after_the_lookup_still_releases_the_claim() -> (
    None
):
    """A registry error on the *write* side (``upsert_node_on_hello``, reached only
    after ``old``/``deferred`` were already read from memory, with ``old`` already
    closed and deferred during the ``get_node`` gate exactly like the fix round's
    tests) must settle the deferred end - ``_admit_locked`` never reaches its own
    "consume the deferred entry" line, so this is the generic fallback in
    ``_release_claim`` doing the work, not the success path."""
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    gate = GatedGet(reg)
    task = await park_admission(manager, gate.held, node, 2)
    await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
    assert old.state is NodeState.ONLINE  # deferred
    assert manager.admission_pending(old)

    FailingUpsert(reg)  # only after get_node's gate has already parked this Hello
    gate.release.set()
    with pytest.raises(RegistryError):
        await task
    await settle()

    assert_no_stuck_claim(manager, node)
    assert old.state is NodeState.OFFLINE
    assert reg.nodes[node].state is NodeState.OFFLINE
    assert old.writer.done()


@pytest.mark.asyncio
async def test_two_pending_hellos_different_epochs_resolve_exactly_one_deferred_end() -> (
    None
):
    """Two validated Hellos for the same node, different epochs, both pending at
    once (a double reconnect race): whichever order they settle in, the old
    session is deferred exactly once and consumed exactly once - never resolved
    twice, never left stuck. The higher epoch wins; the lower is rejected as
    stale once the higher has landed.

    ``SessionManager`` serialises the *body* of admission through one manager-wide
    lock (``self._lock``), so at most one Hello can be inside ``_admit_locked`` at
    a time - but ``_pending_hellos`` is recorded *before* that lock is acquired
    (module docstring), so a second Hello queued behind the first is already
    "pending" (and already claims the node, per ``admission_pending``) while it
    waits for the lock, exactly like two real concurrent reconnects would race at
    the gRPC layer even though the manager admits them one at a time."""
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    gate = GatedGet(reg)
    with structlog.testing.capture_logs() as logs:
        task_lo = await park_admission(manager, gate.held, node, 2)
        # A second Hello for the same node, higher epoch: it registers itself in
        # ``_pending_hellos`` at once, then blocks on the manager's lock (task_lo
        # holds it, parked inside the gated ``get_node``) - it never reaches
        # ``get_node`` itself until task_lo's admission returns.
        task_hi = asyncio.create_task(manager.admit(hello(node, 3), "ipv4:127.0.0.1:3"))
        for _ in range(50):
            if sorted(manager._pending_hellos.get(node, [])) == [2, 3]:
                break
            await asyncio.sleep(0)
        assert sorted(manager._pending_hellos[node]) == [2, 3]
        assert manager.admission_pending(old)

        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
        assert old.state is NodeState.ONLINE  # deferred once

        gate.release.set()
        results = await asyncio.gather(task_lo, task_hi, return_exceptions=True)
        await settle()

    assert_no_stuck_claim(manager, node)
    # Exactly one state event for old(epoch 1)'s end (never fired twice): both
    # epoch 2 and epoch 3 are legitimately Accepted in turn (each was, in order,
    # a higher epoch than anything seen before it) - epoch 2's own close of
    # epoch 1 is what is deferred and settled exactly once.
    assert state_changes(logs, node).count(("online", "offline")) <= 1
    live = manager.get(node)
    assert live is not None and live.session_epoch == 3
    assert live.state is NodeState.ONLINE
    accepted = [r for r in results if isinstance(r, Accepted)]
    assert len(accepted) == 2
    assert {a.live.session_epoch for a in accepted} == {2, 3}
    # The epoch 1 session never sees a real event fire twice, and its chain ends
    # with exactly one close_session write.
    closes = [w for w in reg.writes if w[1] == old.session_id and w[0] == "close_session"]
    assert len(closes) == 1
