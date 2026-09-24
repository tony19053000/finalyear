"""Slow set (D25): V0.3 success criteria 2 and 3 measured at the SHIPPED timings.

Nothing is overridden (heartbeat 2 s, SUSPECT at 2.5 s, OFFLINE at 6 s; agent
backoff base 0.5 s / cap 15 s; keepalive 10 s + 5 s), so these numbers are the
product's, not a test profile's. They are **loopback** numbers - controller,
agents and the fault proxy share one machine - and bound the software, not a
network; the lab run (0.3.15) measures real cable pulls.

The bounds are the criteria themselves, with no extra margin:
* criterion 2: cable pulled -> OFFLINE within 8 s; restored -> ONLINE within 10 s
  with no agent restart (a blackholed FaultLink is the pulled cable);
* criterion 3: controller killed and restarted -> all agents back within 15 s.
Expected on loopback: OFFLINE 4-6 s after the fault (the lease runs from the last
heartbeat, which is up to one interval before it), ONLINE < 1 s after restore,
all agents back in a few seconds. A result near a bound is a finding, not noise.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import SHIPPED, START_TIMEOUT_S, Fabric, MakeFabric

pytestmark = [pytest.mark.docker, pytest.mark.slow]

RecordTiming = Callable[[str, float, "float | None", str], None]

CABLE_OFFLINE_BOUND_S = 8.0
CABLE_ONLINE_BOUND_S = 10.0
CONTROLLER_BACK_BOUND_S = 15.0


def test_slow_d_cable_pull_offline_and_restore_at_shipped_timing(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=SHIPPED)
    victim = fabric.nodes[0]
    pid = victim.pid
    baseline = fabric.transition_counts()

    t0 = time.monotonic()
    victim.link.blackhole()
    offline_s = fabric.wait_state(
        victim, "offline", within=CABLE_OFFLINE_BOUND_S + 5.0, since=t0
    )
    record_timing(
        "slow d cable pull -> OFFLINE", offline_s, CABLE_OFFLINE_BOUND_S, SHIPPED.name
    )

    t1 = time.monotonic()
    victim.link.blackhole(False)
    online_s = fabric.wait_state(
        victim, "online", connected=True, within=CABLE_ONLINE_BOUND_S + 5.0, since=t1
    )
    record_timing(
        "slow d restore -> ONLINE", online_s, CABLE_ONLINE_BOUND_S, SHIPPED.name
    )

    assert offline_s <= CABLE_OFFLINE_BOUND_S
    assert online_s <= CABLE_ONLINE_BOUND_S
    assert victim.pid == pid and victim.restarts == 0
    assert fabric.arrows(victim, baseline) == [
        ("online", "suspect", "lease_missed"),
        ("suspect", "offline", "lease_expired"),
        ("offline", "online", "reconnected"),
    ]
    offline = [
        t for t in fabric.new_transitions(victim, baseline) if t.to_state == "offline"
    ]
    assert offline[0].silence_s is not None
    assert (
        SHIPPED.offline_after_s - 0.001
        <= offline[0].silence_s
        <= SHIPPED.offline_after_s + 0.5
    )
    fabric.assert_quiet([n for n in fabric.nodes if n is not victim], baseline)


def test_slow_i_controller_kill_restart_all_back_at_shipped_timing(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=SHIPPED)
    pids = [n.pid for n in fabric.nodes]

    t0 = fabric.kill_controller()
    t1 = fabric.restart_controller()  # returns once the new one is listening
    listening_s = time.monotonic() - t1
    back_s = fabric.wait_all_connected(within=START_TIMEOUT_S, since=t0)
    record_timing(
        "slow i controller SIGKILL -> all 5 ONLINE",
        back_s,
        CONTROLLER_BACK_BOUND_S,
        SHIPPED.name,
    )
    record_timing(
        "slow i (controller spawn -> listening)", listening_s, None, SHIPPED.name
    )
    assert back_s <= CONTROLLER_BACK_BOUND_S
    assert [n.pid for n in fabric.nodes] == pids
    assert all(n.restarts == 0 for n in fabric.nodes)
