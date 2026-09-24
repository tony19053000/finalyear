"""V0.3 G6 tester round — criterion 2 risk (docs/SESSION_HANDOFF.md §2, "Risk to criterion 2").

V0.3 success criterion 2 requires: link restored -> node ONLINE within <= 10 s, with no agent
restart. The ONLY thing standing between "link restored" and "agent dials again" is whatever is
left of the current :class:`Backoff` sleep. ``next_delay()`` draws
``uniform(0, ceiling(attempt)) with ceiling capped at 15 s`` (``BackoffPolicy`` shipped default:
base 0.5 s, cap 15 s, reset after a 30 s-lived stream — ``agent/session/policy.py``).

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

This is a **design defect against criterion 2**: nothing in the reconnect policy reacts to the
link coming back mid-sleep, and the cap (15 s) exceeds the criterion's bound (10 s) by design, so
the worst case is unbounded-by-the-criterion, not just statistically likely to occasionally
breach it. This test proves the worst case with the *real* ``Backoff`` class end to end (no
mocking of the object under test) by controlling only the injected ``rand`` source, exactly the
seam the class itself defines for testing (see ``test_session_policy.py``). It is intentionally
**strict** ``xfail``: if it ever starts passing, someone tightened the cap or added mid-sleep
link-aware wake-up, and the roadmap criterion-2 risk note should be closed, not this test deleted.
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


def _shipped_policy() -> BackoffPolicy:
    # Mirrors tests/cluster/test_fabric_slow.py's SHIPPED profile: nothing overridden.
    return BackoffPolicy()  # base_s=0.5, cap_s=15.0, reset_after_s=30.0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "design defect: BackoffPolicy.cap_s (15 s, shipped default, and its own field "
        "description names only criterion 3's 15 s bound) exceeds V0.3 criterion 2's "
        "restore -> ONLINE bound (10 s). Once an outage is long enough to have pushed the "
        "attempt counter to the capped rung (~40 s cumulative on shipped settings, once real "
        "per-attempt hello_timeout_s overhead is counted -- see the module docstring), the "
        "agent's next reconnect sleep is uniform(0, 15 s) with NO mid-sleep wake-up on link "
        "recovery, so a draw arbitrarily close to 15 s is a legitimate outcome of the real "
        "interval, not an edge case being forced. For the criterion to be unconditionally true, "
        "cap_s must be <= 10 s (e.g. align it with the criterion, or the client must "
        "reset/shorten the current sleep when a new connection attempt becomes possible)."
    ),
)
def test_worst_case_reconnect_sleep_after_long_outage_can_exceed_criterion_2_bound() -> None:
    clock = FakeClock()
    # `rand` returns values near the top of `[0, 1)` -- a legitimate output of
    # `random.random()`, not an out-of-range stub. This is the real Backoff class;
    # only the RNG seam it defines for tests is controlled.
    near_top = iter([0.999999] * 20)
    backoff = Backoff(_shipped_policy(), clock=clock, rand=lambda: next(near_top))

    # Drive it through enough failed cycles to reach the 15 s-capped rung, as a real
    # 30 s+ outage would (ceilings: 0.5, 1, 2, 4, 8, 15, ... -- attempt 5 is already
    # capped, and the *ceilings alone* sum past 15 s well before a 30 s outage ends).
    delays = [backoff.next_delay() for _ in range(6)]
    assert backoff.attempt == 6
    worst_case_delay = delays[-1]

    # The controller-restore moment can legitimately land at the very start of this
    # sleep (nothing in the policy shortens a sleep already in progress), so the
    # agent's real restore -> ONLINE latency is bounded below by this draw alone,
    # before even accounting for the connect + Hello + Ack round trip on top of it.
    assert worst_case_delay <= CRITERION_2_BOUND_S, (
        f"draw {worst_case_delay:.3f}s at the capped rung already exceeds the "
        f"{CRITERION_2_BOUND_S}s criterion-2 bound, with no reconnect attempt yet made"
    )


def test_ceiling_reaches_the_cap_well_inside_a_30s_outage() -> None:
    """Sanity check for the xfail's premise: the *ceiling* schedule alone (ignoring the real
    per-attempt hello_timeout_s overhead, which the module docstring accounts for separately)
    reaches the 15 s cap on the 6th failed cycle, and the ceilings of the 5 cycles *before* that
    sum to only 15.5 s of *sleep* time -- well under a 30 s outage. This is only half the real
    story (see the module docstring for the measured, overhead-inclusive number); it isolates
    the one fact that is not in question: BackoffPolicy.ceiling really does reach cap_s, and
    does so within a small number of cycles. Not xfail: this is just arithmetic."""
    policy = _shipped_policy()
    ceilings = [policy.ceiling(n) for n in range(6)]
    assert ceilings == [0.5, 1.0, 2.0, 4.0, 8.0, 15.0]
    assert sum(ceilings[:5]) < 30.0
    assert ceilings[5] == policy.cap_s
