"""V0.4 G1 (D41): ``[controller] admission_timeout_s`` - default, bounds (max
accepted AND max+1 rejected) and the "below the OFFLINE edge" rule."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meshforge.controller.settings import ControllerSection, LivenessSettings

pytestmark = [pytest.mark.unit, pytest.mark.security]

# A lease long enough (11 s x 3 = 33 s) that only the field bounds are in play.
LONG_LEASE = LivenessSettings(heartbeat_interval_s=11.0, lease_multiplier=3)


def section(**kw: object) -> ControllerSection:
    return ControllerSection(**kw)  # type: ignore[arg-type]


def test_the_default_is_5_s_below_the_default_6_s_lease() -> None:
    cfg = section()
    assert cfg.admission_timeout_s == 5.0
    assert cfg.admission_timeout_s < cfg.liveness.to_config().offline_after_s


@pytest.mark.parametrize("value", [0.5, 30.0])
def test_the_bounds_are_accepted(value: float) -> None:
    assert section(admission_timeout_s=value, liveness=LONG_LEASE).admission_timeout_s


@pytest.mark.parametrize("value", [0.49, 30.01, 0.0, -1.0])
def test_outside_the_bounds_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError, match="admission_timeout_s"):
        section(admission_timeout_s=value, liveness=LONG_LEASE)


def test_just_below_the_offline_edge_is_accepted() -> None:
    # default lease: 2 s x 3 = 6 s
    assert section(admission_timeout_s=5.99).admission_timeout_s == 5.99


@pytest.mark.parametrize("value", [6.0, 6.01])
def test_at_or_past_the_offline_edge_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError, match="OFFLINE edge"):
        section(admission_timeout_s=value)


def test_an_unset_deadline_follows_a_short_lease() -> None:
    """An operator who only retunes the heartbeat must not trip the rule: unset,
    the deadline is min(5 s, lease - 1 s), never below 0.5 s."""
    fast = LivenessSettings(
        heartbeat_interval_s=0.2, miss_allowance_s=0.05, lease_multiplier=3
    )
    assert section(liveness=fast).admission_timeout_s == 0.5  # lease 0.6 s
    one_s = LivenessSettings(heartbeat_interval_s=1.0)
    assert section(liveness=one_s).admission_timeout_s == 2.0  # lease 3 s
    assert section(liveness={"heartbeat_interval_s": 10.0}).admission_timeout_s == 5.0


def test_an_explicit_deadline_is_never_adjusted() -> None:
    one_s = LivenessSettings(heartbeat_interval_s=1.0)  # lease 3 s
    with pytest.raises(ValidationError, match="OFFLINE edge"):
        section(liveness=one_s, admission_timeout_s=5.0)
