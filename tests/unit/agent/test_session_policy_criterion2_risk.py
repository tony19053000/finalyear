"""V0.3 G6 tester round — criterion 2 risk (docs/SESSION_HANDOFF.md §2, "Risk to criterion 2").

V0.3 success criterion 2 requires: link restored -> node ONLINE within <= 10 s, with no agent
restart. The ONLY thing standing between "link restored" and "agent dials again" is whatever is
left of the current :class:`Backoff` sleep. ``next_delay()`` draws
``uniform(0, ceiling(attempt))`` with the ceiling capped at ``cap_s`` (shipped default since the
G6 fix round, D27: base 0.5 s, cap **5 s** (was 15 s), reset after a 30 s-lived stream —
``agent/settings.py`` ``RECONNECT_*``, used by ``agent/session/policy.py``).

**Status (G6 fix round):** the defect below was real and is fixed by D27 — the cap is 5 s and the
settings validator refuses anything above ``RECONNECT_CAP_BOUND_S``. The former strict ``xfail`` is
now a passing **guard**: it fails if the shipped cap, or the bound the validator allows, is raised
past what criterion 2 tolerates. The history below is kept as the reason the guard exists.

The *ceilings alone* sum to only 15.5 s over the first 5 failed cycles (0.5+1+2+4+8), but each
real cycle also spends ``hello_timeout_s`` (5 s shipped default) failing to get a HelloAck before
the sleep even starts (a ``blackhole`` fault keeps the socket open and silently drops bytes, so
the agent's own Hello-timeout, not a fast connection refusal, is what ends each attempt) - measured
empirically on the cluster harness (``tests/cluster/test_criterion2_long_outage_restore.py``): a
30 s blackhole does NOT reliably reach the capped rung (6 trials, worst restore latency 0.497 s),
and even 60 s is closer to the ~40 s cumulative (5 x hello_timeout + the ceilings) it takes to
reach it than a comfortable margin (6 trials, worst 3.574 s - no violation observed, but the
margin past the capped rung was often small). This unit test does not depend on that arithmetic:
it drives the *ceiling schedule itself* (``test_ceiling_reaches_the_cap_well_inside_a_30s_outage``
below) to show the rung is real, and proves that once ANY outage is long enough to reach it
(architecturally: outages beyond roughly 40 s on the shipped settings, or shorter if the network's
own failure detection is faster than a full ``hello_timeout_s``), the agent's next reconnect sleep
is ``uniform(0, 15 s)`` with no mid-sleep wake-up on link recovery - so a draw close to 15 s is a
legitimate, unforced outcome of the real interval, not an edge case being manufactured.

This was a **design defect against criterion 2**: nothing in the reconnect policy reacts to the
link coming back mid-sleep, and the cap (then 15 s) exceeded the criterion's bound (10 s) by
design, so the worst case was unbounded-by-the-criterion, not just statistically likely to
occasionally breach it. This test drives the worst case with the *real* ``Backoff`` class end
to end (no mocking of the object under test) by controlling only the injected ``rand`` source,
exactly the seam the class itself defines for testing (see ``test_session_policy.py``).
"""

from __future__ import annotations

import pytest

from meshforge.agent.session.policy import Backoff, BackoffPolicy

pytestmark = pytest.mark.unit

CRITERION_2_BOUND_S = 10.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


RESTORE_ROUND_TRIP_MARGIN_S = 5.0
"""What criterion 2 must leave after the sleep for connect + Hello + HelloAck (D27: a 5 s cap
keeps the worst case at ~6 s). The guard below fails if the cap eats into it."""


def _shipped_policy() -> BackoffPolicy:
    # Mirrors tests/cluster/test_fabric_slow.py's SHIPPED profile: nothing overridden.
    return BackoffPolicy()  # base_s=0.5, cap_s=5.0 (D27), reset_after_s=30.0


def test_worst_case_reconnect_sleep_after_long_outage_leaves_criterion_2_margin() -> (
    None
):
    clock = FakeClock()
    # `rand` returns values near the top of `[0, 1)` -- a legitimate output of
    # `random.random()`, not an out-of-range stub. This is the real Backoff class;
    # only the RNG seam it defines for tests is controlled.
    near_top = iter([0.999999] * 20)
    backoff = Backoff(_shipped_policy(), clock=clock, rand=lambda: next(near_top))

    # Drive it through enough failed cycles to reach the capped rung, as a long outage
    # would (ceilings: 0.5, 1, 2, 4, 5, ... -- attempt 4 is already capped).
    delays = [backoff.next_delay() for _ in range(6)]
    assert backoff.attempt == 6
    worst_case_delay = delays[-1]

    # The link can come back at the very start of this sleep (nothing shortens a sleep
    # already in progress), so the restore -> ONLINE latency is this draw plus the
    # connect + Hello round trip. It must leave that round trip its margin.
    assert worst_case_delay <= CRITERION_2_BOUND_S - RESTORE_ROUND_TRIP_MARGIN_S, (
        f"draw {worst_case_delay:.3f}s at the capped rung leaves less than "
        f"{RESTORE_ROUND_TRIP_MARGIN_S}s of the {CRITERION_2_BOUND_S}s criterion-2 bound"
    )


def test_the_settings_validator_refuses_a_cap_past_the_criterion_2_bound() -> None:
    """The shipped default alone is not enough: an operator's ``[agent.reconnect] cap_s``
    must not be able to reopen the defect either (D27)."""
    from pydantic import ValidationError

    from meshforge.agent.settings import RECONNECT_CAP_BOUND_S, ReconnectSettings

    assert RECONNECT_CAP_BOUND_S <= CRITERION_2_BOUND_S - RESTORE_ROUND_TRIP_MARGIN_S
    assert ReconnectSettings().cap_s == _shipped_policy().cap_s <= RECONNECT_CAP_BOUND_S
    ReconnectSettings(cap_s=RECONNECT_CAP_BOUND_S)  # the bound itself is allowed
    with pytest.raises(ValidationError, match="cap_s"):
        ReconnectSettings(cap_s=RECONNECT_CAP_BOUND_S + 0.01)


def test_ceiling_reaches_the_cap_within_a_few_cycles() -> None:
    """Sanity check for the guard's premise: the *ceiling* schedule alone reaches the cap on
    the 5th failed cycle, and the ceilings before that sum to only 7.5 s of sleep -- a short
    outage already puts the agent on the capped rung. Just arithmetic."""
    policy = _shipped_policy()
    ceilings = [policy.ceiling(n) for n in range(6)]
    assert ceilings == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]
    assert sum(ceilings[:4]) < 30.0
    assert ceilings[4] == policy.cap_s
