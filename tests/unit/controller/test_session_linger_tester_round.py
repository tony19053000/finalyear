"""V0.3 G6 tester round — two linger/fence scenarios the existing
``test_session_linger.py`` suite does not cover (tester brief priority 3):
two reconnects in quick succession, and a slow/gated database write racing a
linger's resolution. Reuses that module's harness (``FencedRegistry``, ``make``,
``admit``) rather than duplicating it.
"""

from __future__ import annotations

import asyncio

import pytest
from test_session_linger import (
    CFG,
    NOW0,
    admit,
    make,
    settle,
)

from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_two_reconnects_in_quick_succession_leave_a_consistent_row() -> None:
    """session1 lingers (abrupt end) -> session2 resolves it -> session2 itself
    ends abruptly and starts lingering -> session3 resolves it, all with no
    ticks of the clock in between (as a flapping Wi-Fi radio might do). Each
    step's fence must see exactly the previous session, never a stale one, and
    the final row must read ONLINE under session3."""
    manager, reg, _clock, _timer = make()
    node_id = "node_" + "0" * 26

    s1 = await admit(manager, node_id, 1)
    await manager.end_session(s1, SessionEndReason.STREAM_LOST)
    assert s1.lingering and manager.get_lingering(node_id) is s1

    s2 = await admit(manager, node_id, 2)
    assert not s1.lingering and manager.get_lingering(node_id) is None
    assert s2.state is NodeState.ONLINE

    await manager.end_session(s2, SessionEndReason.STREAM_LOST)
    assert s2.lingering and manager.get_lingering(node_id) is s2
    # session1's own (now-stale) chain must not still be able to touch the row.
    assert not s1.lingering

    s3 = await admit(manager, node_id, 3)
    assert not s2.lingering and manager.get_lingering(node_id) is None
    assert s3.state is NodeState.ONLINE

    await settle()
    assert reg.nodes[node_id].state is NodeState.ONLINE
    assert reg.nodes[node_id].last_session_id == s3.session_id
    # No session's fenced write for a lower session id ever finds 0 rows landed
    # AFTER a higher session has legitimately taken over and then also must not
    # corrupt a later session's row: every OFFLINE attempted in this sequence
    # (s1's and s2's linger writes, none of which reached the lease edge) must
    # never have landed once a newer session existed.
    offline_landed = [
        w for w in reg.writes if w[0] == "set_node_state" and w[2] is NodeState.OFFLINE
    ]
    assert offline_landed == []


@pytest.mark.asyncio
async def test_slow_database_write_during_linger_resolution_leaves_the_row_honest() -> (
    None
):
    """The lease-edge OFFLINE write for a lingering entry is gated (a slow /
    momentarily unavailable database), and a reconnect resolves the linger
    while that write is still in flight. Once the gate opens and the stale
    write actually lands on the database, D20's fence must have already made
    it a no-op: the row must end up ONLINE under the new session, never
    clobbered back to OFFLINE by the late write."""
    manager, reg, clock, timer = make()
    node_id = "node_" + "1" * 26

    old = await admit(manager, node_id, 1)
    await manager.end_session(old, SessionEndReason.STREAM_LOST)
    assert old.lingering

    # Gate the database so the lease-edge OFFLINE write cannot complete yet.
    reg.gate.clear()
    edge = NOW0 + CFG.offline_after_s
    clock.now = edge
    fire = asyncio.create_task(timer.tick(edge))
    # Give the timer a chance to reach set_node_state and block on the gate.
    for _ in range(20):
        await asyncio.sleep(0)
        if reg.state_calls_started >= 1:
            break
    assert reg.state_calls_started >= 1, "the OFFLINE write never started"

    # While the database is still stuck on that write, the agent reconnects.
    new = await admit(manager, node_id, 2)
    assert new.state is NodeState.ONLINE
    assert manager.get_lingering(node_id) is None  # resolved without an OFFLINE

    # Now let the database "catch up": the stale write finally lands.
    reg.gate.set()
    await asyncio.wait_for(fire, 5.0)
    await settle()

    assert reg.nodes[node_id].state is NodeState.ONLINE
    assert reg.nodes[node_id].last_session_id == new.session_id
    stale_write = [
        w for w in reg.writes if w[0] == "set_node_state" and w[1] == old.session_id
    ]
    assert stale_write and stale_write[-1][3] is False, (
        "the late OFFLINE write from the resolved linger must be fenced out "
        f"(landed=False), got {stale_write}"
    )
