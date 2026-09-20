"""Task 0.3.7 (logic half): §12.3 node state machine and the lease tracker.

Every (state, event) pair is enumerated; the clock is a fake the test advances by
hand - no ``sleep``, no wall clock, no asyncio.
"""

from __future__ import annotations

import itertools
import pickle

import pytest

from meshforge.common.errors import FailureType
from meshforge.controller.liveness import (
    InvalidTransitionError,
    LeaseTracker,
    LivenessConfig,
    LivenessEvent,
    NodeState,
    next_state,
)

pytestmark = pytest.mark.unit

S, E = NodeState, LivenessEvent

#: The §12.3 arrows (plus the ONLINE heartbeat self-loop). Everything else must raise.
ARROWS: dict[tuple[NodeState, LivenessEvent], NodeState] = {
    (S.ENROLLING, E.HELLO_ACCEPTED): S.ONLINE,
    (S.ONLINE, E.HEARTBEAT): S.ONLINE,
    (S.ONLINE, E.LEASE_MISSED): S.SUSPECT,
    (S.SUSPECT, E.HEARTBEAT): S.ONLINE,
    (S.SUSPECT, E.LEASE_EXPIRED): S.OFFLINE,
    (S.OFFLINE, E.RECONNECTED): S.ONLINE,
    (S.ONLINE, E.DRAIN): S.DRAINING,
    (S.DRAINING, E.UNDRAIN): S.ONLINE,
    (S.ONLINE, E.REVOKE): S.REVOKED,
    (S.OFFLINE, E.REVOKE): S.REVOKED,
}

ALL_PAIRS = list(itertools.product(NodeState, LivenessEvent))


def test_enums_match_architecture_12_3() -> None:
    assert [s.name for s in NodeState] == [
        "ENROLLING",
        "ONLINE",
        "SUSPECT",
        "OFFLINE",
        "DRAINING",
        "REVOKED",
    ]
    assert len(LivenessEvent) == 8
    assert len(ALL_PAIRS) == 48
    assert NodeState.ONLINE == "online"  # StrEnum: DB/JSON value is the lowercase name


@pytest.mark.parametrize(("state", "event"), ALL_PAIRS, ids=lambda x: x.name)
def test_every_pair_is_an_arrow_or_invalid(
    state: NodeState, event: LivenessEvent
) -> None:
    expected = ARROWS.get((state, event))
    if expected is not None:
        assert next_state(state, event) is expected
    else:
        with pytest.raises(InvalidTransitionError) as info:
            next_state(state, event)
        err = info.value
        assert (err.state, err.event) == (state, event)
        assert err.failure_type is FailureType.CONFLICT
        assert not err.retryable
        assert err.details == {"state": state.value, "event": event.value}
        assert state.value in str(err) and event.value in str(err)


def test_arrow_table_is_exactly_ten_arrows_and_nothing_leaves_revoked() -> None:
    valid = {pair for pair in ALL_PAIRS if pair in ARROWS}
    assert len(valid) == 10
    for event in LivenessEvent:
        with pytest.raises(InvalidTransitionError):
            next_state(S.REVOKED, event)


def test_invalid_transition_pickles() -> None:
    err = InvalidTransitionError(S.REVOKED, E.HEARTBEAT)
    back = pickle.loads(pickle.dumps(err))
    assert isinstance(back, InvalidTransitionError)
    assert str(back) == str(err)
    assert back.details == err.details


# ------------------------------------------------------------------------ config


def test_default_config_matches_adr_0002() -> None:
    cfg = LivenessConfig()
    assert cfg.heartbeat_interval_s == 2.0
    assert cfg.suspect_after_s == 2.5
    assert cfg.offline_after_s == 6.0
    assert cfg.offline_after_s <= 8.0  # V0.3 criterion 2


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"heartbeat_interval_s": 0}, "heartbeat_interval_s"),
        ({"heartbeat_interval_s": -1.0}, "heartbeat_interval_s"),
        ({"miss_allowance_s": -0.1}, "miss_allowance_s"),
        ({"lease_multiplier": 0}, "lease_multiplier"),
        # multiplier 1 -> offline 2.0 <= suspect 2.5
        ({"lease_multiplier": 1}, "offline_after_s"),
        # large allowance pushes suspect past offline
        ({"miss_allowance_s": 4.0}, "offline_after_s"),
        ({"heartbeat_interval_s": float("nan")}, "heartbeat_interval_s"),
    ],
)
def test_config_validation(kwargs: dict[str, float], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        LivenessConfig(**kwargs)


def test_config_is_frozen() -> None:
    cfg = LivenessConfig()
    with pytest.raises(AttributeError):
        cfg.heartbeat_interval_s = 1.0  # type: ignore[misc]


# ----------------------------------------------------------------------- tracker


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_walk_through_silence_emits_each_event_once() -> None:
    clock = FakeClock()
    tracker = LeaseTracker(LivenessConfig(), clock)
    expected = {
        0.0: None,
        2.4: None,
        2.6: E.LEASE_MISSED,
        5.9: None,
        6.0: E.LEASE_EXPIRED,
    }
    for t, event in expected.items():
        clock.now = t
        assert tracker.check() is event, t
    # Long after: nothing more for this silence, no matter how often we poll.
    for t in (6.0, 6.1, 60.0, 3600.0):
        assert tracker.check(t) is None, t
    assert tracker.deadline() is None


def test_explicit_now_overrides_clock() -> None:
    clock = FakeClock(100.0)
    tracker = LeaseTracker(LivenessConfig(), clock)
    assert tracker.check(102.4) is None
    assert tracker.check(102.6) is E.LEASE_MISSED
    assert tracker.check(102.7) is None
    assert tracker.check(106.0) is E.LEASE_EXPIRED


def test_edges_are_inclusive_so_a_timer_at_deadline_sees_the_event() -> None:
    tracker = LeaseTracker(LivenessConfig(), FakeClock(10.0))
    assert tracker.deadline() == 12.5
    assert tracker.check(12.5) is E.LEASE_MISSED
    assert tracker.deadline() == 16.0
    assert tracker.check(16.0) is E.LEASE_EXPIRED
    assert tracker.deadline() is None


def test_heartbeat_during_suspect_resets_the_silence() -> None:
    clock = FakeClock()
    tracker = LeaseTracker(LivenessConfig(), clock)
    clock.now = 3.0
    assert tracker.check() is E.LEASE_MISSED
    state = next_state(S.ONLINE, E.LEASE_MISSED)
    assert state is S.SUSPECT

    clock.now = 3.5
    tracker.observe_heartbeat()
    state = next_state(state, E.HEARTBEAT)
    assert state is S.ONLINE
    assert tracker.last_heartbeat == 3.5
    assert tracker.silence(3.5) == 0.0
    assert tracker.deadline() == 6.0  # 3.5 + 2.5

    # The old expiry (0 + 6.0) must NOT fire: the silence started over at 3.5.
    clock.now = 5.9
    assert tracker.check() is None  # 2.4 s since 3.5
    clock.now = 6.0
    assert tracker.check() is E.LEASE_MISSED  # 2.5 s: a fresh miss, not an expiry
    clock.now = 9.4
    assert tracker.check() is None
    clock.now = 9.5
    assert tracker.check() is E.LEASE_EXPIRED  # 3.5 + 6.0


def test_after_heartbeat_the_events_fire_again_once_each() -> None:
    tracker = LeaseTracker(LivenessConfig(), FakeClock())
    assert tracker.check(3.0) is E.LEASE_MISSED
    tracker.observe_heartbeat(3.5)
    assert tracker.check(5.9) is None  # 2.4 s
    assert tracker.check(6.0) is E.LEASE_MISSED  # 2.5 s
    assert tracker.check(6.1) is None
    assert tracker.check(9.4) is None  # 5.9 s
    assert tracker.check(9.5) is E.LEASE_EXPIRED  # 6.0 s
    assert tracker.check(9.6) is None


def test_unpolled_tracker_reports_missed_before_expired() -> None:
    # If the caller never polled during the SUSPECT window (e.g. a stalled loop),
    # the state machine must still walk ONLINE -> SUSPECT -> OFFLINE.
    tracker = LeaseTracker(LivenessConfig(), FakeClock())
    state = S.ONLINE
    assert tracker.check(30.0) is E.LEASE_MISSED
    state = next_state(state, E.LEASE_MISSED)
    assert tracker.check(30.0) is E.LEASE_EXPIRED
    state = next_state(state, E.LEASE_EXPIRED)
    assert state is S.OFFLINE
    assert tracker.check(30.0) is None


def test_deadline_math() -> None:
    cfg = LivenessConfig(
        heartbeat_interval_s=1.0, miss_allowance_s=0.25, lease_multiplier=4
    )
    assert (cfg.suspect_after_s, cfg.offline_after_s) == (1.25, 4.0)
    tracker = LeaseTracker(cfg, FakeClock(7.0))
    assert tracker.deadline() == 8.25
    tracker.observe_heartbeat(9.0)
    assert tracker.deadline() == 10.25
    assert tracker.check(10.25) is E.LEASE_MISSED
    assert tracker.deadline() == 13.0
    assert tracker.check(12.99) is None
    assert tracker.check(13.0) is E.LEASE_EXPIRED
    assert tracker.deadline() is None
    tracker.observe_heartbeat(20.0)
    assert tracker.deadline() == 21.25
    assert tracker.config is cfg


def test_stale_heartbeat_timestamp_never_moves_lease_backwards() -> None:
    tracker = LeaseTracker(LivenessConfig(), FakeClock())
    tracker.observe_heartbeat(10.0)
    tracker.observe_heartbeat(4.0)  # out-of-order caller; clock is monotonic
    assert tracker.last_heartbeat == 10.0
    assert tracker.silence(9.0) == 0.0  # clamped, never negative


def test_tracker_never_touches_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def forbidden() -> float:
        raise AssertionError("wall clock used")

    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    tracker = LeaseTracker(LivenessConfig(), FakeClock(1.0))
    tracker.observe_heartbeat()
    assert tracker.check() is None
    assert tracker.deadline() == 3.5
