"""Cluster scenarios h and i: agent and controller restarts on real processes (0.3.14).

h - V0.3 criterion 4: an agent restarted on the same data dir keeps its
    ``node_id``, comes back with a higher session epoch, and is ONLINE again.
i - criterion 3's shape: the controller is SIGKILLed and restarted on *new* ports
    (every link retargeted, as a controller laptop with a new DHCP port would
    not do, but a new process does); all five agents reconnect with no agent
    restarted, and the sessions the dead controller left open are closed
    ``controller_restart`` by the start-up sweep.

Fast profile (D25). The bounds are the criteria's own (15 s) - generous on
loopback; the slow set (``test_fabric_slow.py``) measures them at shipped timing.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import FAST, START_TIMEOUT_S, Fabric, MakeFabric

pytestmark = [pytest.mark.docker]

RecordTiming = Callable[[str, float, "float | None", str], None]

RECONNECT_ALL_WITHIN_S = 15.0
"""V0.3 criterion 3 bound; loopback reconnects take ~1-2 s (G3)."""


@pytest.mark.parametrize("graceful", [True, False], ids=["sigint", "sigkill"])
def test_h_agent_restart_keeps_node_id_with_higher_epoch(
    make_fabric: MakeFabric, record_timing: RecordTiming, graceful: bool
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    victim = fabric.nodes[2]
    node_id, old_pid = victim.node_id, victim.pid
    before = fabric.node_detail(victim)
    assert before is not None
    baseline = fabric.transition_counts()

    t0 = victim.restart(graceful=graceful)
    started = fabric.wait_log(victim.process, "agent.started", within=START_TIMEOUT_S)
    assert started["node_id"] == node_id  # same data dir -> same identity
    back_s = fabric.wait_state(
        victim, "online", connected=True, within=START_TIMEOUT_S, since=t0
    )

    after = fabric.node_detail(victim)
    assert after is not None
    assert after["node_id"] == node_id
    assert after["session_epoch"] > before["session_epoch"]
    assert after["last_session_id"] != before["last_session_id"]
    assert victim.pid != old_pid and victim.restarts == 1
    # Exactly one node in the fabric for this identity - no duplicate enrolment.
    views = fabric.nodes_by_id()
    assert views is not None and len(views) == 5

    arrows = fabric.arrows(victim, baseline)
    assert arrows[-1] == ("offline", "online", "reconnected"), arrows
    if graceful:
        # A clean close is OFFLINE at once (with FAST timing a lease_missed
        # SUSPECT may precede it inside the half-close window - see g).
        assert ("online", "offline", "stream_closed") in arrows or (
            "suspect",
            "offline",
            "stream_closed",
        ) in arrows, arrows
    else:
        assert arrows[:2] == [
            ("online", "suspect", "stream_lost"),
            ("suspect", "offline", "lease_expired"),
        ], arrows
    fabric.assert_quiet([n for n in fabric.nodes if n is not victim], baseline)
    record_timing(
        f"h agent restart ({'SIGINT' if graceful else 'SIGKILL'}) -> ONLINE",
        back_s,
        None,
        FAST.name,
    )


def test_i_controller_sigkill_restart_all_agents_back_none_restarted(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    pids = {n.node_id: n.pid for n in fabric.nodes}
    old_sessions = {}
    for node in fabric.nodes:
        detail = fabric.node_detail(node)
        assert detail is not None
        old_sessions[node.node_id] = detail["last_session_id"]
    old_grpc = fabric.grpc_target

    t0 = fabric.kill_controller()
    t1 = fabric.restart_controller()
    assert fabric.grpc_target != old_grpc  # new process, new port, links retargeted
    back_since_kill = fabric.wait_all_connected(within=RECONNECT_ALL_WITHIN_S, since=t0)
    back_since_start = back_since_kill - (t1 - t0)

    for node in fabric.nodes:
        assert node.pid == pids[node.node_id] and node.restarts == 0
        old = fabric.session(node, old_sessions[node.node_id])
        assert old["end_reason"] == "controller_restart", old
        detail = fabric.node_detail(node)
        assert detail is not None
        assert detail["last_session_id"] != old_sessions[node.node_id]
        # The new controller saw each node come back exactly once.
        new_gen = [t for t in fabric.transitions(node) if t.generation == 1]
        assert [(t.from_state, t.to_state) for t in new_gen] == [
            ("offline", "online")
        ], new_gen
    record_timing(
        "i controller SIGKILL -> all ONLINE", back_since_kill, 15.0, FAST.name
    )
    record_timing("i controller start -> all ONLINE", back_since_start, None, FAST.name)
