"""V0.3 G6 tester round — criterion 2 risk, measured on real processes (docs/SESSION_HANDOFF.md
§2, "Risk to criterion 2"; tester brief priority 1).

``tests/cluster/test_fabric_slow.py`` measures restore -> ONLINE after a SHORT blackhole (the
fault fires, the wait times out at ``CABLE_OFFLINE_BOUND_S`` = 8 s, so the blackhole itself has
only been up for a few seconds when it is lifted). This module blackholes a link for the full
30 s and 60 s the tester brief asks for, at the SHIPPED timing profile (no overrides), and
records the real restore -> ONLINE latency over several trials — long enough for the agent's
full-jitter backoff (base 0.5 s, cap 5 s since D27 - it was 15 s when this was written - reset
after a 30 s-lived stream) to have reached the capped rung before the link comes back (see
``tests/unit/agent/test_session_policy_criterion2_risk.py``: at 15 s that rung alone could exceed
the 10 s criterion bound; at 5 s it cannot).

This is a **measurement**, not a pass/fail gate: the draw is genuine full-jitter randomness in a
real subprocess (there is no seam to control it from the test, unlike the unit-level proof), so a
run that happens to draw a small delay will legitimately meet the bound. What is recorded here is
the exact number per trial, printed via ``record_timing`` and this module's own summary, plus
whether the agent restarted (it must not, per the criterion). A run that violates the bound is
reported as a finding in the tester's report, not asserted away or silently accepted — the
`record_timing` table in the pytest output is the evidence either way.
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
TRIALS = 3
# Generous ceiling so a slow draw is *observed*, not mistaken for a hang: worst case is
# the cap (5 s since D27; 15 s before) plus connect/Hello/Ack overhead.
WAIT_CEILING_S = 30.0


def _one_trial(
    fabric: Fabric, outage_s: float, record_timing: Callable[..., None], label: str
) -> tuple[float, bool]:
    victim = fabric.nodes[0]
    pid_before = victim.pid
    t0 = time.monotonic()
    victim.link.blackhole()
    fabric.wait_state(victim, "offline", within=outage_s + 15.0, since=t0)

    time.sleep(max(0.0, outage_s - (time.monotonic() - t0)))
    t1 = time.monotonic()
    victim.link.blackhole(False)
    online_s = fabric.wait_state(
        victim, "online", connected=True, within=WAIT_CEILING_S, since=t1
    )
    record_timing(label, online_s, CRITERION_2_BOUND_S, SHIPPED.name)
    restarted = victim.pid != pid_before or victim.restarts != 0
    return online_s, restarted


def test_restore_after_30s_blackhole_over_several_trials(
    make_fabric: MakeFabric, record_timing: Callable[..., None]
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=SHIPPED)
    results = []
    for i in range(TRIALS):
        online_s, restarted = _one_trial(
            fabric, 30.0, record_timing, f"criterion2 30s outage trial {i}"
        )
        results.append(online_s)
        assert not restarted, f"trial {i}: agent restarted during a 30 s outage"
    print(  # noqa: T201 - the measured table is this module's evidence (-s shows it)
        f"\n[criterion2] 30s outage restore->ONLINE over {TRIALS} trials: "
        f"{results} (bound {CRITERION_2_BOUND_S}s, "
        f"mean={statistics.mean(results):.3f}s, worst={max(results):.3f}s)"
    )
    exceeded = [r for r in results if r > CRITERION_2_BOUND_S]
    if exceeded:
        print(  # noqa: T201 - a finding must be visible in the run output
            f"[criterion2] FINDING: {len(exceeded)}/{TRIALS} trials exceeded the "
            f"{CRITERION_2_BOUND_S}s bound after a 30s outage: {exceeded}"
        )


def test_restore_after_60s_blackhole_over_several_trials(
    make_fabric: MakeFabric, record_timing: Callable[..., None]
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=SHIPPED)
    results = []
    for i in range(TRIALS):
        online_s, restarted = _one_trial(
            fabric, 60.0, record_timing, f"criterion2 60s outage trial {i}"
        )
        results.append(online_s)
        assert not restarted, f"trial {i}: agent restarted during a 60 s outage"
    print(  # noqa: T201 - the measured table is this module's evidence (-s shows it)
        f"\n[criterion2] 60s outage restore->ONLINE over {TRIALS} trials: "
        f"{results} (bound {CRITERION_2_BOUND_S}s, "
        f"mean={statistics.mean(results):.3f}s, worst={max(results):.3f}s)"
    )
    exceeded = [r for r in results if r > CRITERION_2_BOUND_S]
    if exceeded:
        print(  # noqa: T201 - a finding must be visible in the run output
            f"[criterion2] FINDING: {len(exceeded)}/{TRIALS} trials exceeded the "
            f"{CRITERION_2_BOUND_S}s bound after a 60s outage: {exceeded}"
        )
