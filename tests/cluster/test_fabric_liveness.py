"""Cluster scenarios: liveness of real agent processes under real faults (0.3.14).

Five ``meshforge-agent`` processes against one ``meshforge-controller`` process,
fast timing profile (D25: heartbeat 0.2 s -> SUSPECT at 0.25 s, OFFLINE at 0.6 s
from the last heartbeat). Everything is observed through REST ``/v1/nodes`` and
the processes' JSON logs; the ``make_fabric`` fixture ends every scenario with the
invariant check (no forbidden events, no tracebacks, no unrequested restarts).

Timing margins (why these numbers):

* ``EDGE_SLACK_S`` - a ``LEASE_EXPIRED`` is logged with ``silence_s`` (time since
  the last heartbeat on the controller's monotonic clock). It must be at least
  the lease and at most ``EDGE_SLACK_S`` later: the single lease-timer task
  sleeps until the earliest deadline, so lateness is event-loop scheduling in a
  controller process that also serves 5 streams and the REST polls. 0.25 s is
  ~40 % of the lease - generous for loopback, tight enough that a timer that
  waited a whole extra lease (0.6 s) fails.
* ``REST_SLACK_S`` - fault -> state visible over REST adds the fenced DB write
  and the 50 ms poll; 1.5 s absorbs a loaded CI box without hiding a missed edge
  (the next edge would be another full lease away... or never).
* ``QUIET_WINDOW_S`` - "no collateral transitions" is judged over two further
  leases after the victim went OFFLINE: a false SUSPECT on a healthy node needs
  only 0.25 s of silence, so two leases give it ample room to show up.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import sys
import time
from collections.abc import Callable

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import FAST, Fabric, MakeFabric, Node

pytestmark = [pytest.mark.docker]

RecordTiming = Callable[[str, float, "float | None", str], None]

EDGE_SLACK_S = 0.25
REST_SLACK_S = 1.5
RECONNECT_WITHIN_S = 10.0
QUIET_WINDOW_S = 2 * FAST.offline_after_s
OFFLINE_WITHIN_S = FAST.offline_after_s + REST_SLACK_S


def _others(fabric: Fabric, victim: Node) -> list[Node]:
    return [n for n in fabric.nodes if n is not victim]


def _assert_offline_at_lease_edge(
    fabric: Fabric, victim: Node, baseline: dict[str, int]
) -> None:
    offline = [
        t for t in fabric.new_transitions(victim, baseline) if t.to_state == "offline"
    ]
    assert len(offline) == 1, fabric.arrows(victim, baseline)
    silence = offline[0].silence_s
    assert silence is not None
    # silence_s is logged rounded to 1 ms, hence the 0.001 on the lower bound.
    lease = FAST.offline_after_s
    assert lease - 0.001 <= silence <= lease + EDGE_SLACK_S, (
        f"OFFLINE logged at silence {silence} s, lease {FAST.offline_after_s} s"
    )


def test_a_enrol_five_distinct_nodes_online_connected_with_samples(
    make_fabric: MakeFabric,
) -> None:
    fabric: Fabric = make_fabric(
        agents=5,
        timing=FAST,
        agent_resources={"sample_interval_s": 0.5, "max_report_interval_s": 1.0},
    )
    ids = [n.node_id for n in fabric.nodes]
    assert len(set(ids)) == 5 and all(i.startswith("node_") for i in ids)

    def samples_flowing() -> bool:
        views = fabric.nodes_by_id()
        return views is not None and all(
            views[i]["sample_age_s"] is not None and views[i]["sample_age_s"] < 3.0
            for i in ids
        )

    fabric.wait_until(
        samples_flowing, within=15.0, what="every node has a fresh sample"
    )
    views = fabric.nodes_by_id()
    assert views is not None
    for node in fabric.nodes:
        view = views[node.node_id]
        assert (view["state"], view["connected"]) == ("online", True)
        # sample_age_s: max_report_interval 1 s + one sample interval + poll.
        assert view["sample_age_s"] < 3.0
        # Enrolment is exactly one arrow per node.
        assert fabric.arrows(node, {}) == [("enrolling", "online", "hello_accepted")]


def test_b_sigkill_one_agent_suspect_then_offline_others_untouched(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    victim = fabric.nodes[2]
    baseline = fabric.transition_counts()

    t0 = victim.kill()
    elapsed = fabric.wait_state(victim, "offline", within=OFFLINE_WITHIN_S, since=t0)
    assert elapsed <= OFFLINE_WITHIN_S
    assert fabric.arrows(victim, baseline) == [
        ("online", "suspect", "stream_lost"),
        ("suspect", "offline", "lease_expired"),
    ]
    _assert_offline_at_lease_edge(fabric, victim, baseline)
    view = fabric.node_view(victim)
    assert view is not None and view["connected"] is False
    lost = fabric.new_transitions(victim, baseline)[0].session_id
    assert fabric.session(victim, lost)["end_reason"] == "stream_lost"

    time.sleep(QUIET_WINDOW_S)
    fabric.assert_quiet(_others(fabric, victim), baseline)
    fabric.wait_all_connected(within=1.0, nodes=_others(fabric, victim))
    record_timing("b SIGKILL -> OFFLINE", elapsed, OFFLINE_WITHIN_S, FAST.name)


def test_c_pause_past_lease_offline_then_resume_online_same_pid(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    victim = fabric.nodes[1]
    pid, node_id = victim.pid, victim.node_id
    epoch_before = (fabric.node_detail(victim) or {})["session_epoch"]
    baseline = fabric.transition_counts()

    t0 = victim.pause()
    try:
        offline_s = fabric.wait_state(
            victim, "offline", within=OFFLINE_WITHIN_S, since=t0
        )
        _assert_offline_at_lease_edge(fabric, victim, baseline)
    finally:
        t1 = victim.resume()
    online_s = fabric.wait_state(
        victim, "online", connected=True, within=RECONNECT_WITHIN_S, since=t1
    )

    assert victim.pid == pid and victim.process.running
    detail = fabric.node_detail(victim)
    assert detail is not None and detail["node_id"] == node_id
    assert detail["session_epoch"] > epoch_before
    assert fabric.arrows(victim, baseline) == [
        ("online", "suspect", "lease_missed"),
        ("suspect", "offline", "lease_expired"),
        ("offline", "online", "reconnected"),
    ]
    fabric.assert_quiet(_others(fabric, victim), baseline)
    record_timing("c pause -> OFFLINE", offline_s, OFFLINE_WITHIN_S, FAST.name)
    record_timing("c resume -> ONLINE", online_s, RECONNECT_WITHIN_S, FAST.name)


def test_d_blackhole_offline_at_lease_edge_restore_online_no_restart(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    victim = fabric.nodes[3]
    pid = victim.pid
    baseline = fabric.transition_counts()

    t0 = time.monotonic()
    victim.link.blackhole()
    offline_s = fabric.wait_state(victim, "offline", within=OFFLINE_WITHIN_S, since=t0)
    _assert_offline_at_lease_edge(fabric, victim, baseline)
    view = fabric.node_view(victim)
    assert view is not None and view["connected"] is False

    t1 = time.monotonic()
    victim.link.blackhole(False)
    online_s = fabric.wait_state(
        victim, "online", connected=True, within=RECONNECT_WITHIN_S, since=t1
    )
    assert victim.pid == pid and victim.process.running and victim.restarts == 0
    assert fabric.arrows(victim, baseline) == [
        ("online", "suspect", "lease_missed"),
        ("suspect", "offline", "lease_expired"),
        ("offline", "online", "reconnected"),
    ]
    lost = fabric.new_transitions(victim, baseline)[0].session_id
    assert fabric.session(victim, lost)["end_reason"] == "lease_expired"
    assert victim.link.counters().bytes_dropped > 0  # the blackhole really ate bytes
    fabric.assert_quiet(_others(fabric, victim), baseline)
    record_timing("d blackhole -> OFFLINE", offline_s, OFFLINE_WITHIN_S, FAST.name)
    record_timing("d restore -> ONLINE", online_s, RECONNECT_WITHIN_S, FAST.name)


def test_e_reset_peer_suspect_then_online_without_offline(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    """THE G4 harness gap: the controller stays up and the agent's TCP is reset.

    D19: an abrupt stream loss is SUSPECT with a lingering lease; a reconnect that
    beats the lease edge must never produce OFFLINE.
    """
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    victim = fabric.nodes[0]
    pid = victim.pid
    baseline = fabric.transition_counts()
    sessions_before = len(fabric.sessions(victim))

    t0 = time.monotonic()
    victim.link.reset_peer()

    def reconnected() -> bool:
        return ("suspect", "online", "reconnected") in fabric.arrows(victim, baseline)

    back_s = fabric.wait_until(
        reconnected, within=RECONNECT_WITHIN_S, what="reconnect", since=t0
    )
    fabric.wait_state(victim, "online", connected=True, within=REST_SLACK_S)
    arrows = fabric.arrows(victim, baseline)
    assert arrows == [
        ("online", "suspect", "stream_lost"),
        ("suspect", "online", "reconnected"),
    ], arrows
    assert all(
        t.to_state != "offline" for t in fabric.new_transitions(victim, baseline)
    )
    sessions = fabric.sessions(victim)
    assert len(sessions) == sessions_before + 1
    lost = fabric.new_transitions(victim, baseline)[0].session_id
    assert fabric.session(victim, lost)["end_reason"] == "stream_lost"
    assert victim.pid == pid
    # The linger ended because the new session arrived, not because the lease ran out.
    linger_ends = [
        r
        for r in fabric.controller_records()
        if r.get("event") == "session.linger_ended"
        and r.get("node_id") == victim.node_id
    ]
    assert not any(r["cause"] == "lease_expired" for r in linger_ends)
    time.sleep(QUIET_WINDOW_S)
    assert fabric.arrows(victim, baseline) == arrows  # no late OFFLINE either
    fabric.assert_quiet(_others(fabric, victim), baseline)
    record_timing(
        "e reset_peer -> ONLINE (no OFFLINE)", back_s, RECONNECT_WITHIN_S, FAST.name
    )


def test_g_graceful_stop_offline_promptly_connected_false(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    """NB-G5-1 on real processes: a clean close is OFFLINE now, not at a lease."""
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    victim = fabric.nodes[4]
    baseline = fabric.transition_counts()

    t0, code = victim.stop_graceful()
    assert code == 0
    offline_s = fabric.wait_state(
        victim, "offline", connected=False, within=OFFLINE_WITHIN_S, since=t0
    )
    arrows = fabric.arrows(victim, baseline)
    record_timing("g SIGINT -> OFFLINE", offline_s, OFFLINE_WITHIN_S, FAST.name)
    # What must hold (G6-A follow-up 4): it ends OFFLINE via stream_closed, with no
    # ONLINE in between. Since D40 the Goodbye is decided on receipt (no 0.25 s
    # confirmation window); a SUSPECT before it is still tolerated here because a
    # loaded box can delay the agent's signal handling past FAST's 0.25 s edge -
    # test_l pins the single arrow.
    assert arrows[-1] == ("online", "offline", "stream_closed") or arrows[-1] == (
        "suspect",
        "offline",
        "stream_closed",
    ), arrows
    assert all(to != "online" for _f, to, _e in arrows), arrows
    assert len(arrows) <= 2, arrows
    closed = fabric.new_transitions(victim, baseline)[-1].session_id
    assert fabric.session(victim, closed)["end_reason"] == "agent_closed"
    time.sleep(QUIET_WINDOW_S)
    assert fabric.arrows(victim, baseline) == arrows
    fabric.assert_quiet(_others(fabric, victim), baseline)


def test_j_latency_below_miss_allowance_causes_no_transitions(
    make_fabric: MakeFabric,
) -> None:
    """False-positive guard: slow-but-alive links must not flap any node."""
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    baseline = fabric.transition_counts()
    # 20 ms each way + up to 20 ms jitter: the extra silence a heartbeat can see
    # is at most the jitter (constant latency shifts every heartbeat equally),
    # well below the 50 ms miss allowance.
    for node in fabric.nodes:
        node.link.latency(20, jitter_ms=20)
    time.sleep(5 * FAST.offline_after_s)
    fabric.wait_all_connected(within=1.0)
    fabric.assert_quiet(fabric.nodes, baseline)
    for node in fabric.nodes:
        node.link.latency(0)


def test_g2_graceful_stop_is_a_single_arrow_when_allowance_exceeds_confirm_window(
    make_fabric: MakeFabric,
) -> None:
    """With the shipped-like miss allowance (0.3 s here, 0.5 s shipped), a clean
    stop is exactly one arrow, ONLINE -> OFFLINE (stream_closed) - no SUSPECT
    blip. (Written for the V0.3 half-close confirmation window; since D40 the
    Goodbye makes it hold with no window at all.)"""
    timing = dataclasses.replace(FAST, name="fast-allowance-0.3", miss_allowance_s=0.3)
    fabric: Fabric = make_fabric(agents=1, timing=timing)
    victim = fabric.nodes[0]
    baseline = fabric.transition_counts()
    t0, code = victim.stop_graceful()
    assert code == 0
    fabric.wait_state(
        victim, "offline", connected=False, within=OFFLINE_WITHIN_S, since=t0
    )
    assert fabric.arrows(victim, baseline) == [("online", "offline", "stream_closed")]


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM is TerminateProcess on Windows"
)
def test_l_sigterm_goodbye_offline_within_1s_sigkill_suspect_then_lease(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    """D40 on real processes, side by side. SIGTERM = a requested stop: the agent
    says Goodbye and REST shows OFFLINE within 1 s, one arrow, ``agent_closed``.
    SIGKILL = no Goodbye: SUSPECT first, OFFLINE only at the lease edge."""
    fabric: Fabric = make_fabric(agents=2, timing=FAST)
    clean, killed = fabric.nodes[0], fabric.nodes[1]
    baseline = fabric.transition_counts()

    t0 = time.monotonic()
    clean.process.expected_alive = False
    os.kill(clean.pid, signal.SIGTERM)
    offline_s = fabric.wait_state(
        clean, "offline", connected=False, within=1.0, since=t0
    )
    assert clean.process.wait_exit(15.0) == 0
    record_timing("l SIGTERM -> OFFLINE", offline_s, 1.0, FAST.name)
    assert fabric.arrows(clean, baseline) == [("online", "offline", "stream_closed")]
    closed = fabric.new_transitions(clean, baseline)[-1].session_id
    assert fabric.session(clean, closed)["end_reason"] == "agent_closed"

    t1 = killed.kill()
    fabric.wait_state(killed, "offline", within=OFFLINE_WITHIN_S, since=t1)
    view = fabric.node_view(killed)
    assert view is not None and view["connected"] is False
    assert fabric.arrows(killed, baseline) == [
        ("online", "suspect", "stream_lost"),
        ("suspect", "offline", "lease_expired"),
    ]
    _assert_offline_at_lease_edge(fabric, killed, baseline)
