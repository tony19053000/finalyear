"""V0.3 G6 tester round 2: criterion 2 at the shipped D27/D29 timings, extending
``test_criterion2_long_outage_restore.py`` (docs/briefs/V0.3-fabric-core.md §5f,
priority 2).

Two shapes the fix round's own measurement did not cover:

1. a longer outage (~120 s) - long enough that the agent's backoff exponent has
   certainly reset at least once (``reset_after_s`` only resets after a *lived*
   stream, so it never resets during a dead outage) and is sitting at the capped
   rung for most of the wait, so the worst-case draw the unit-level guard proves
   (``test_session_policy_criterion2_risk.py``) is exercised on a real subprocess,
   not simulated;
2. the link returning while a reconnect attempt is already **in flight** on the
   dead path (mid ``hello_timeout_s``), not only while the agent is asleep -
   the worst case for this shape is a full ``hello_timeout_s`` wasted on the dead
   attempt, THEN the queued backoff sleep, THEN the successful attempt, so it can
   be worse than a restore that lands mid-sleep.

Like the fix round's module, this is a **measurement**: the real interleaving of
a live subprocess and the FaultLink proxy is not seeded, so a run that lands on a
small draw legitimately meets the bound. Every number is recorded via
``record_timing``; a violation is a finding, not a silent skip.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import SHIPPED, Fabric, MakeFabric

pytestmark = [pytest.mark.docker, pytest.mark.slow]

CRITERION_2_BOUND_S = 10.0
WAIT_CEILING_S = 30.0


@pytest.mark.timeout(
    600
)  # 2 trials x (120 s outage + up to 30 s wait) > the 300 s default
def test_restore_after_120s_blackhole_over_several_trials(
    make_fabric: MakeFabric, record_timing: Callable[..., None]
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=SHIPPED)
    results = []
    trials = (
        2  # each trial is a genuine 120 s wait; kept small to fit the harness's budget
    )
    for i in range(trials):
        victim = fabric.nodes[0]
        pid_before = victim.pid
        t0 = time.monotonic()
        victim.link.blackhole()
        fabric.wait_state(victim, "offline", within=120.0 + 15.0, since=t0)
        time.sleep(max(0.0, 120.0 - (time.monotonic() - t0)))
        t1 = time.monotonic()
        victim.link.blackhole(False)
        online_s = fabric.wait_state(
            victim, "online", connected=True, within=WAIT_CEILING_S, since=t1
        )
        record_timing(
            f"criterion2 120s outage trial {i}",
            online_s,
            CRITERION_2_BOUND_S,
            SHIPPED.name,
        )
        results.append(online_s)
        assert victim.pid == pid_before and victim.restarts == 0, (
            f"trial {i}: agent restarted during a 120 s outage"
        )
    print(  # noqa: T201 - the measured table is this module's evidence (-s shows it)
        f"\n[criterion2] 120s outage restore->ONLINE over {trials} trials: "
        f"{results} (bound {CRITERION_2_BOUND_S}s, "
        f"mean={statistics.mean(results):.3f}s, worst={max(results):.3f}s)"
    )
    exceeded = [r for r in results if r > CRITERION_2_BOUND_S]
    if exceeded:
        print(  # noqa: T201 - a finding must be visible in the run output
            f"[criterion2] FINDING: {len(exceeded)}/{trials} trials exceeded the "
            f"{CRITERION_2_BOUND_S}s bound after a 120s outage: {exceeded}"
        )


def test_restore_while_a_reconnect_attempt_is_in_flight_on_the_dead_path(
    make_fabric: MakeFabric, record_timing: Callable[..., None]
) -> None:
    """The link comes back *during* a dead-path attempt (mid ``hello_timeout_s``,
    3 s shipped since D29), not while the agent is asleep: the restore->ONLINE
    latency measured from the moment the link is un-blackholed must still include
    however much of the wasted attempt (and any queued sleep after it) is left."""
    fabric: Fabric = make_fabric(agents=5, timing=SHIPPED)
    victim = fabric.nodes[0]
    pid_before = victim.pid
    t0 = time.monotonic()
    victim.link.blackhole()
    fabric.wait_state(victim, "offline", within=15.0, since=t0)
    # Give the agent time to be mid-attempt on the dead path: several
    # hello_timeout_s + backoff cycles in, but not so long the exact phase
    # matters - the point is the restore lands while *something* is in flight,
    # not during a fresh cold start.
    time.sleep(20.0)
    t1 = time.monotonic()
    victim.link.blackhole(False)
    online_s = fabric.wait_state(
        victim, "online", connected=True, within=WAIT_CEILING_S, since=t1
    )
    record_timing(
        "criterion2 restore mid-attempt", online_s, CRITERION_2_BOUND_S, SHIPPED.name
    )
    assert victim.pid == pid_before and victim.restarts == 0
    if online_s > CRITERION_2_BOUND_S:
        print(  # noqa: T201 - a finding must be visible in the run output
            f"[criterion2] FINDING: restore mid-attempt took {online_s:.3f}s, "
            f"over the {CRITERION_2_BOUND_S}s bound"
        )
