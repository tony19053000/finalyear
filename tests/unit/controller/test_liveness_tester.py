"""Independent tester round for roadmap 0.3.7 (logic half): §12.3 liveness.

Adversarial edge cases derived from ARCHITECTURE §12.3 and ADR-0002 Updates §5,
written without first reading ``tests/unit/controller/test_liveness.py``.
Never edits ``src/``.
"""

from __future__ import annotations

import math

import pytest

from meshforge.controller.liveness import (
    InvalidTransitionError,
    LeaseTracker,
    LivenessConfig,
    LivenessEvent,
    NodeState,
)

pytestmark = pytest.mark.unit

S, E = NodeState, LivenessEvent


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------------- config


def test_config_zero_miss_allowance_is_valid_edge() -> None:
    cfg = LivenessConfig(miss_allowance_s=0.0)
    assert cfg.suspect_after_s == cfg.heartbeat_interval_s


@pytest.mark.parametrize(
    "kwargs",
    [
        {"heartbeat_interval_s": float("inf")},
        {"miss_allowance_s": float("inf")},
        {"heartbeat_interval_s": float("nan")},
        {"miss_allowance_s": float("nan")},
    ],
)
def test_config_rejects_inf_and_nan_everywhere_not_just_heartbeat(
    kwargs: dict[str, float],
) -> None:
    """inf/NaN must never silently produce a config with offline <= suspect or a
    NaN deadline; every non-finite input should raise ValueError, the same
    guarantee the implementer's own test gives for ``heartbeat_interval_s=nan``.
    """
    with pytest.raises(ValueError):
        LivenessConfig(**kwargs)


def test_config_accepts_huge_but_finite_multiplier() -> None:
    cfg = LivenessConfig(lease_multiplier=1_000_000)
    assert cfg.offline_after_s == pytest.approx(2.0 * 1_000_000)


def test_config_lease_multiplier_as_bool_is_technically_a_valid_int() -> None:
    """``bool`` is a subclass of ``int`` in Python; ``lease_multiplier=True`` (1)
    passes ``> 0`` and silently becomes multiplier 1, which then fails the
    offline > suspect check (2.0 <= 2.5) - documenting that this is caught
    downstream, not by a type check on the field itself.
    """
    with pytest.raises(ValueError, match="offline_after_s"):
        LivenessConfig(lease_multiplier=True)  # type: ignore[arg-type]


# --------------------------------------------------------------- boundary instants


def test_check_one_ulp_before_suspect_boundary_is_none() -> None:
    cfg = LivenessConfig()
    tracker = LeaseTracker(cfg, FakeClock())
    just_before = math.nextafter(cfg.suspect_after_s, 0.0)
    assert tracker.check(just_before) is None
    assert tracker.check(cfg.suspect_after_s) is E.LEASE_MISSED


def test_check_one_ulp_before_offline_boundary_is_none() -> None:
    cfg = LivenessConfig()
    tracker = LeaseTracker(cfg, FakeClock())
    tracker.check(cfg.suspect_after_s)  # consume the MISSED event first
    just_before = math.nextafter(cfg.offline_after_s, 0.0)
    assert tracker.check(just_before) is None
    assert tracker.check(cfg.offline_after_s) is E.LEASE_EXPIRED


def test_deadline_is_monotonic_non_decreasing_across_a_silence_then_heartbeat() -> None:
    cfg = LivenessConfig()
    clock = FakeClock()
    tracker = LeaseTracker(cfg, clock)
    seen = []
    for t in (0.0, 1.0, 2.5, 2.6, 4.0, 6.0):
        clock.now = t
        tracker.check()
        d = tracker.deadline()
        if d is not None:
            seen.append(d)
    # Within one silence, deadline never decreases.
    assert seen == sorted(seen)
    tracker.observe_heartbeat(7.0)
    after_heartbeat = tracker.deadline()
    assert after_heartbeat == 7.0 + cfg.suspect_after_s
    assert after_heartbeat > seen[-1] - cfg.offline_after_s  # sane, re-armed forward


def test_check_with_now_before_construction_time_is_defined_not_negative() -> None:
    """The clock is documented monotonic, but ``check`` must not leak an exception
    or a negative silence if a caller ever passes an earlier ``now`` directly
    (never via ``observe_heartbeat``, which is separately guarded)."""
    tracker = LeaseTracker(LivenessConfig(), FakeClock(100.0))
    # No observe_heartbeat call; last_heartbeat = 100.0 from construction.
    result = tracker.check(50.0)
    assert result is None
    assert tracker.silence(50.0) == 0.0


# ---------------------------------------------------------------- state machine


def test_online_heartbeat_self_loop_is_the_only_self_loop() -> None:
    for state in NodeState:
        if state is NodeState.ONLINE:
            continue
        for event in LivenessEvent:
            try:
                from meshforge.controller.liveness import next_state

                result = next_state(state, event)
            except InvalidTransitionError:
                continue
            assert result is not state, f"unexpected self-loop {state}/{event}"


def test_revoked_is_a_true_sink_for_every_single_event() -> None:
    from meshforge.controller.liveness import next_state

    for event in LivenessEvent:
        with pytest.raises(InvalidTransitionError):
            next_state(NodeState.REVOKED, event)


def test_enrolling_only_accepts_hello_accepted() -> None:
    from meshforge.controller.liveness import next_state

    for event in LivenessEvent:
        if event is LivenessEvent.HELLO_ACCEPTED:
            assert next_state(NodeState.ENROLLING, event) is NodeState.ONLINE
        else:
            with pytest.raises(InvalidTransitionError):
                next_state(NodeState.ENROLLING, event)
