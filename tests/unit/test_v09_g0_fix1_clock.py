"""V0.9 G0 fix round 1 (Lead ruling: the measure never silently passes on a
bad clock): ping filtering, the before/after phase clock, the verdict on it."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from mf_ui_perf import load_ui_perf

from meshforge.common.clock_offset import (
    ClockExchange,
    ClockOffset,
    ClockOffsetError,
    estimate_offset,
)

pytestmark = pytest.mark.unit

PERF = load_ui_perf("ui_perf_v09_g0_fix1")


class _Page:
    def __init__(self, *answers: list[Any]) -> None:
        self.answers = list(answers)

    def evaluate(self, _script: str, _arg: Any = None) -> list[Any]:
        return self.answers.pop(0)


def _ok(t: float, skew: float = 0.0) -> list[float]:
    return [t, t + 1.0 + skew, t + 2.0]


def _clock(
    offset: float = 0.0, bound: float = 1.5, *, consistent: bool = True
) -> ClockOffset:
    return ClockOffset(offset, bound, 2 * bound, 16, consistent)


# ------------------------------------------------------------------ pings


def test_one_backwards_ping_is_dropped_and_counted_not_fatal() -> None:
    rows = [_ok(float(10 * i)) for i in range(15)] + [[500.0, 501.0, 499.0]]
    clock, dropped = PERF.ping_clock(_Page(rows), pings=16)
    assert dropped == 1 and clock.exchanges == 15 and clock.consistent


def test_failed_null_and_non_finite_pings_are_dropped() -> None:
    rows: list[Any] = [_ok(float(10 * i)) for i in range(12)]
    rows += [None, [1.0, None, 2.0], [1.0, math.nan, 2.0], "garbage"]
    _, dropped = PERF.ping_clock(_Page(rows), pings=16)
    assert dropped == 4


def test_too_few_good_pings_is_an_error_the_phase_turns_into_a_reason() -> None:
    rows = [_ok(0.0)] * 7 + [None] * 9  # 7 < 16 // 2
    with pytest.raises(ClockOffsetError):
        PERF.ping_clock(_Page(rows), pings=16)
    block = PERF.phase_clock(
        "only 7 of 16 clock pings usable (need 8)", _clock(), [10.0]
    )
    assert (
        block["uncertain"] and "before: clock measurement failed" in block["reasons"][0]
    )
    json.dumps(block)  # still a report, not a traceback


def test_a_clock_step_inside_one_measurement_is_inconsistent_and_wide() -> None:
    rows = [_ok(float(10 * i)) for i in range(8)]
    rows += [_ok(float(100 + 10 * i), skew=5000.0) for i in range(8)]
    clock, _ = PERF.ping_clock(_Page(rows), pings=16)
    assert not clock.consistent and clock.bound_ms >= 2500.0
    assert PERF.clock_report(clock)["uncertain"] is True


def test_agreeing_pings_keep_the_min_rtt_bound() -> None:
    clock = estimate_offset(
        [ClockExchange(0.0, 1.0, 2.0), ClockExchange(10.0, 15.0, 20.0)],
        resolution_ms=1.0,
    )
    assert clock.consistent and clock.bound_ms == pytest.approx(1.5)


# --------------------------------------------------------- before / after


def test_a_steady_clock_is_certain_and_reports_both_measurements() -> None:
    block = PERF.phase_clock(_clock(0.5), _clock(0.7), [120.0, 15.0])
    assert block["reasons"] == [] and block["uncertain"] is False
    assert block["before"]["clock_offset_ms"] == 0.5
    assert block["after"]["clock_offset_ms"] == 0.7
    assert block["clock_offset_ms"] == 0.5 and block["clock_offset_bound_ms"] == 1.5


def test_the_offset_moving_during_the_phase_is_uncertain() -> None:
    block = PERF.phase_clock(_clock(0.0), _clock(5000.0), [120.0])
    assert block["uncertain"] and "offset moved" in block["reasons"][0]


def test_moves_within_the_combined_bound_are_fine() -> None:
    assert not PERF.phase_clock(_clock(0.0), _clock(2.9), [1.0])["uncertain"]
    assert PERF.phase_clock(_clock(0.0), _clock(3.1), [1.0])["uncertain"]


def test_a_latency_below_minus_bound_is_uncertain() -> None:
    assert PERF.phase_clock(_clock(), _clock(), [-1.4])["uncertain"] is False
    block = PERF.phase_clock(_clock(), _clock(), [-1.6, 100.0])
    assert block["uncertain"] and "below -bound" in block["reasons"][0]


def test_the_worse_bound_is_the_one_reported_and_judged() -> None:
    block = PERF.phase_clock(_clock(bound=2.0), _clock(bound=60.0), [10.0])
    assert block["clock_offset_bound_ms"] == 60.0 and block["uncertain"]


# ------------------------------------------------------------------ verdict


def _phase(clock: dict[str, Any], lags: list[float]) -> dict[str, Any]:
    fast = PERF.summary([10.0])
    return {
        "list": {
            "list_first_render": fast,
            "list_in_app_nav": fast,
            "filter_change": fast,
            "detail_open": fast,
            "scroll": {"long_task_max": 0.0},
        },
        "events": {
            "clock": clock,
            "heartbeat_latency": PERF.summary(lags),
            "job_state_latency": PERF.summary(lags),
            "job_state_missed": 0,
        },
    }


def test_verdict_follows_the_phase_clock() -> None:
    good = PERF.phase_clock(_clock(), _clock(), [100.0])
    moved = PERF.phase_clock(_clock(), _clock(9000.0), [100.0])
    assert PERF.verdict({"a": _phase(good, [100.0])}) == "pass"
    assert PERF.verdict({"a": _phase(moved, [100.0])}) == "skew_uncertain"


def test_no_samples_with_a_good_clock_is_a_fail() -> None:
    good = PERF.phase_clock(_clock(), _clock(), [])
    assert PERF.verdict({"a": _phase(good, [])}) == "fail"


# ------------------------------------------------ fix round 2 (tester r2)


def _strict(block: dict[str, Any]) -> Any:
    def refuse(token: str) -> Any:
        raise ValueError(token)

    return json.loads(json.dumps(block, allow_nan=False), parse_constant=refuse)


def test_a_failed_measurement_reports_a_null_bound_never_infinity() -> None:
    for before, after in (("ping failed", _clock()), (_clock(), "ping failed")):
        block = PERF.phase_clock(before, after, [1.0])
        assert block["clock_offset_bound_ms"] is None and block["uncertain"]
        assert _strict(block)["clock_offset_bound_ms"] is None
        assert PERF.verdict({"a": _phase(block, [1.0])}) == "skew_uncertain"


def test_a_non_finite_measurement_value_is_null_in_the_report() -> None:
    report = PERF.clock_report(ClockOffset(0.0, math.inf, math.inf, 3))
    assert report["clock_offset_bound_ms"] is None and report["uncertain"]
    _strict(report)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_lag_is_a_reason_and_never_hides_a_negative_one(
    bad: float,
) -> None:
    only_bad = PERF.phase_clock(_clock(), _clock(), [bad, 100.0])
    assert only_bad["uncertain"] and "not finite" in only_bad["reasons"][0]
    hidden = PERF.phase_clock(_clock(), _clock(), [bad, -900.0])
    assert any("below -bound" in r for r in hidden["reasons"])


def test_more_than_a_quarter_of_one_measurements_pings_dropped_is_uncertain() -> None:
    assert not PERF.phase_clock(_clock(), _clock(), [1.0], dropped=(4, 4))["uncertain"]
    block = PERF.phase_clock(_clock(), _clock(), [1.0], dropped=(0, 5))
    assert block["uncertain"] and "5 clock pings dropped" in block["reasons"][0]
    assert block["pings_dropped"] == 5


def test_a_dropped_total_is_judged_over_both_measurements() -> None:
    assert not PERF.phase_clock(_clock(), _clock(), [1.0], dropped=8)["uncertain"]
    assert PERF.phase_clock(_clock(), _clock(), [1.0], dropped=9)["uncertain"]
