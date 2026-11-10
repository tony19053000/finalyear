"""V1.0 G9-B tester: state that flaps - the D434 clock alert around 250 ms, the
D433 env-policy fingerprint changing mid-session, exclude-node list limits."""

from __future__ import annotations

import pytest

from meshforge.controller.jobs.env_health import KnownBadEnvs
from meshforge.controller.jobs.planning import excluded_nodes
from meshforge.controller.session.clocks import (
    ALERT_BOUND_MS,
    MIN_EXCHANGES,
    NodeClock,
    uncertain_reasons,
)

pytestmark = pytest.mark.unit

WALL0 = 1_792_238_400_000.0  # epoch ms


def _feed(
    clock: NodeClock, n: int, *, t0: float, rtt_ms: float, offset_ms: float = 0.0
) -> list[object]:
    changes: list[object] = []
    for i in range(n):
        sent = t0 + i * 5000.0
        # the agent stamped at the midpoint of the exchange, shifted by offset
        agent = sent + rtt_ms / 2 + offset_ms
        change = clock.observe(
            sent_wall_ms=sent,
            rtt_ms=rtt_ms,
            agent_ms=agent,
            agent_resolution_ms=0.001,
            local_resolution_ms=0.0,
        )
        if change is not None:
            changes.append(change)
    return changes


def test_good_clock_with_slew_never_alerts_but_flip_is_flagged() -> None:
    clock = NodeClock()
    changes = _feed(clock, MIN_EXCHANGES + 2, t0=WALL0, rtt_ms=40.0)
    assert changes == [] and not clock.uncertain
    # NTP step of 70 ms: intervals become disjoint -> inconsistent, bound widens
    changes += _feed(clock, 3, t0=WALL0 + 100_000, rtt_ms=40.0, offset_ms=70.0)
    assert changes == []  # a 70 ms-ish bound is far under 250
    assert clock.latest is not None
    assert not clock.uncertain
    assert clock.latest.bound_ms <= ALERT_BOUND_MS


def test_bound_alert_is_entered_once_and_cleared_once() -> None:
    clock = NodeClock()
    # a huge RTT makes the bound > 250 (bound ~ rtt/2 + resolution)
    entered = _feed(clock, MIN_EXCHANGES + 3, t0=WALL0, rtt_ms=800.0)
    assert len(entered) == 1  # one alert for the episode, not one per ping
    assert clock.uncertain
    # many good exchanges fill the window; the bound comes back under the limit
    left: list[object] = []
    left += _feed(clock, 40, t0=WALL0 + 1_000_000, rtt_ms=20.0)
    assert len(left) == 1 and not clock.uncertain


def test_threshold_is_strictly_greater_than_250() -> None:
    clock = NodeClock()
    _feed(clock, MIN_EXCHANGES, t0=WALL0, rtt_ms=2 * ALERT_BOUND_MS)
    assert clock.latest is not None
    at = clock.latest.bound_ms
    assert uncertain_reasons(clock.latest) == (
        ("bound_over_limit",) if at > ALERT_BOUND_MS else ()
    )


def test_alternating_rtt_straddling_the_limit_does_not_alert_on_every_ping() -> None:
    """The bound is computed over the whole window (a slow exchange in it keeps
    it wide), so a link that flaps between fast and slow pings must not produce
    an alert per flip."""
    clock = NodeClock()
    alerts = 0
    for i in range(60):
        rtt = 600.0 if i % 2 else 30.0
        alerts += len(_feed(clock, 1, t0=WALL0 + i * 5000.0, rtt_ms=rtt))
    assert alerts <= 2  # entered (and at worst left) - never ping-by-ping


def test_flapping_consistency_alone_never_raises_a_change() -> None:
    clock = NodeClock()
    changes: list[object] = []
    flips = 0
    for i in range(80):
        offset = 70.0 if (i // 4) % 2 else 0.0  # step back and forth
        changes += _feed(clock, 1, t0=WALL0 + i * 5000.0, rtt_ms=30.0, offset_ms=offset)
        flips += clock.consistency_flipped
    assert changes == []
    assert not clock.uncertain


def test_exchanges_below_min_never_alert_even_with_huge_bound() -> None:
    clock = NodeClock()
    assert _feed(clock, MIN_EXCHANGES - 1, t0=WALL0, rtt_ms=5000.0) == []


# --------------------------------------------------------- D433 mid-session


class _Mono:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def _bad(policy: str | None) -> tuple[KnownBadEnvs, _Mono]:
    mono = _Mono()
    kb = KnownBadEnvs(ttl_s=600.0, max_entries=8, clock=mono)
    kb.record(
        "n1", "k", env_hash="h", job_id="job_x", attempt_id="att_x", env_policy=policy
    )
    return kb, mono


def test_fingerprint_changes_mid_session_drops_entry_and_counts_once() -> None:
    kb, _ = _bad("a" * 12)
    assert kb.is_bad("n1", "k", env_policy="a" * 12)
    assert not kb.is_bad("n1", "k", env_policy="b" * 12)
    assert kb.cleared == 1
    # flipping back does not resurrect the entry
    assert not kb.is_bad("n1", "k", env_policy="a" * 12)
    assert kb.cleared == 1


def test_unknown_on_either_side_keeps_the_entry_for_its_ttl() -> None:
    kb, _ = _bad(None)
    assert kb.is_bad("n1", "k", env_policy="b" * 12)
    kb2, mono2 = _bad("a" * 12)
    assert kb2.is_bad("n1", "k", env_policy=None)  # older agent / not connected
    mono2.now += 601
    assert not kb2.is_bad("n1", "k", env_policy=None)


def test_a_fresh_failure_after_the_change_is_remembered_under_the_new_one() -> None:
    kb, _ = _bad("a" * 12)
    assert not kb.is_bad("n1", "k", env_policy="b" * 12)
    kb.record(
        "n1", "k", env_hash="h", job_id="job_y", attempt_id="att_y", env_policy="b" * 12
    )
    assert kb.is_bad("n1", "k", env_policy="b" * 12)
    assert not kb.is_bad("n1", "k", env_policy="a" * 12)  # and now A clears it


def test_other_nodes_and_keys_are_untouched_by_a_clear() -> None:
    kb, _ = _bad("a" * 12)
    kb.record(
        "n2", "k", env_hash="h", job_id="job_y", attempt_id="att_y", env_policy="a" * 12
    )
    assert not kb.is_bad("n1", "k", env_policy="c" * 12)
    assert kb.is_bad("n2", "k", env_policy="a" * 12)


# ------------------------------------------------------------ exclude limits


class _Job:
    def __init__(self, placement: dict[str, object]) -> None:
        self.placement = placement


def test_exclude_list_survives_retry_reads_and_is_never_widened() -> None:
    placement = {"exclude_nodes": ["node_b", "node_a", "node_b"]}
    first = excluded_nodes(_Job(placement))  # type: ignore[arg-type]
    again = excluded_nodes(_Job(dict(placement)))  # type: ignore[arg-type]
    assert first == again == ("node_a", "node_b")


@pytest.mark.parametrize("bad", [None, 7, {"a": 1}, "node_a", [[]], [1, 2]])
def test_malformed_stored_values_never_raise(bad: object) -> None:
    assert excluded_nodes(_Job({"exclude_nodes": bad})) == ()  # type: ignore[arg-type]


# ------------------------------------------------------------- API shape


def test_api_exclude_nodes_validation() -> None:
    from meshforge.common.errors import FailureType
    from meshforge.common.ids import new_node_id
    from meshforge.controller.api.errors import ApiError
    from meshforge.controller.api.job_routes import _excluded_nodes

    a, b = str(new_node_id()), str(new_node_id())
    assert _excluded_nodes([b, a, b], None) == tuple(sorted({a, b}))
    with pytest.raises(ApiError) as pinned:
        _excluded_nodes([a], a)
    assert pinned.value.http_status == 422
    assert pinned.value.failure_type is FailureType.INVALID_INPUT
    with pytest.raises(ApiError) as bad:
        _excluded_nodes(["not-a-node"], None)
    assert bad.value.http_status == 400
    assert _excluded_nodes([], a) == ()


def test_api_model_caps_exclude_nodes_at_16() -> None:
    from pydantic import ValidationError

    from meshforge.controller.api.job_models import PlacementRequest

    ok = PlacementRequest(exclude_nodes=[f"node_{i}" for i in range(16)])
    assert len(ok.exclude_nodes) == 16
    with pytest.raises(ValidationError):
        PlacementRequest(exclude_nodes=[f"node_{i}" for i in range(17)])
