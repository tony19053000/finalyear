"""V0.9 G0 tester round 2 (delta on fix round 1): adversarial probes of the
interval-overlap estimator, the before/after phase clock, ping dropping, the
JSON the report writes, and the wheel-offline classifier.

``DEFECT-Rn`` strict xfails pin defects found in round 2.
"""

from __future__ import annotations

import json
import math
import random
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

PERF = load_ui_perf("ui_perf_v09_g0_tester_r2")


def _clock(
    offset: float = 0.0, bound: float = 1.5, *, consistent: bool = True
) -> ClockOffset:
    return ClockOffset(offset, bound, 2 * bound, 16, consistent)


class _Page:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def evaluate(self, _script: str, _arg: Any = None) -> list[Any]:
        return self.rows


def _good(i: int, skew: float = 0.0) -> list[float]:
    t = 10.0 * i
    return [t, t + 1.0 + skew, t + 2.0]


# ----------------------------------------------------------- estimator


def _simulate(
    rng: random.Random, truth_at: Any, n: int, *, tick: float = 1.0
) -> tuple[list[ClockExchange], list[float]]:
    """Pings with arbitrary asymmetric delays; the local stamps are floored to
    ``tick``. ``truth_at(local_time)`` is remote - local at that moment."""
    out: list[ClockExchange] = []
    truths: list[float] = []
    now = 1.76e12
    for _ in range(n):
        d_out, d_back = rng.uniform(0.0, 60.0), rng.uniform(0.0, 60.0)
        sent_true = now
        stamp_true = sent_true + d_out
        received_true = stamp_true + d_back
        truth = truth_at(stamp_true)
        out.append(
            ClockExchange(
                math.floor(sent_true / tick) * tick,
                stamp_true + truth,
                math.floor(received_true / tick) * tick,
            )
        )
        truths.append(truth)
        now = received_true + rng.uniform(1.0, 30.0)
    return out, truths


@pytest.mark.parametrize("seed", range(40))
def test_a_constant_offset_is_never_called_inconsistent_and_stays_contained(
    seed: int,
) -> None:
    rng = random.Random(seed)  # noqa: S311
    truth = rng.choice([0.0, 1234.5, -86_400_000.0, 3.7e6])
    exchanges, _ = _simulate(rng, lambda _t: truth, 16)
    got = estimate_offset(exchanges, resolution_ms=1.0)
    assert got.consistent, "false alarm on a clock that held still"
    assert abs(got.offset_ms - truth) <= got.bound_ms + 1e-6


@pytest.mark.parametrize("seed", range(60))
def test_a_big_step_is_always_inconsistent_and_the_wide_bound_contains_both(
    seed: int,
) -> None:
    rng = random.Random(1000 + seed)  # noqa: S311
    step = rng.choice([-1, 1]) * rng.uniform(150.0, 20_000.0)
    # the clock steps part-way through the series (never before ping 2 / after
    # ping n-2: a step needs pings on both sides)
    at = rng.randrange(2, 14)
    exchanges, _ = _simulate(rng, lambda _t: 0.0, 16)
    stepped = [
        ClockExchange(
            e.sent_ms, e.remote_ms + (step if i >= at else 0.0), e.received_ms
        )
        for i, e in enumerate(exchanges)
    ]
    got = estimate_offset(stepped, resolution_ms=1.0)
    assert not got.consistent
    for truth in (0.0, step):
        assert abs(got.offset_ms - truth) <= got.bound_ms + 1e-6, "truth escaped"


@pytest.mark.parametrize("seed", range(40))
def test_a_small_step_may_pass_but_the_best_pings_truth_is_always_contained(
    seed: int,
) -> None:
    """Below the noise a step is unobservable; the contract then is only that
    the estimate contains the truth at the time of the ping it chose."""
    rng = random.Random(5000 + seed)  # noqa: S311
    step = rng.uniform(-4.0, 4.0)
    exchanges, _ = _simulate(rng, lambda _t: 0.0, 16)
    at = rng.randrange(1, 15)
    stepped = [
        ClockExchange(
            e.sent_ms, e.remote_ms + (step if i >= at else 0.0), e.received_ms
        )
        for i, e in enumerate(exchanges)
    ]
    got = estimate_offset(stepped, resolution_ms=1.0)
    best = min(range(16), key=lambda i: (stepped[i].rtt_ms, i))
    truth_best = step if best >= at else 0.0
    assert abs(got.offset_ms - truth_best) <= got.bound_ms + 1e-6


def test_slow_drift_across_one_measurement_is_consistent_fast_drift_is_not() -> None:
    def series(ms_per_ping: float) -> list[ClockExchange]:
        return [
            ClockExchange(10.0 * i, 10.0 * i + 1.0 + ms_per_ping * i, 10.0 * i + 2.0)
            for i in range(16)
        ]

    slow = estimate_offset(series(0.01), resolution_ms=1.0)  # 0.15 ms total
    assert slow.consistent and slow.bound_ms == pytest.approx(1.5)
    fast = estimate_offset(series(5.0), resolution_ms=1.0)  # 75 ms total
    assert not fast.consistent and fast.bound_ms >= 37.0


def test_the_agreement_edge_is_the_overlap_boundary_not_a_wide_fudge() -> None:
    # two exchanges, rtt 2 -> half-width 1 each (tick 0): they touch at 2.0
    a = ClockExchange(0.0, 1.0, 2.0)  # interval [0, 2]
    touching = ClockExchange(0.0, 3.0, 2.0)  # interval [2, 4]
    apart = ClockExchange(0.0, 3.01, 2.0)  # interval [2.01, 4.01]
    assert estimate_offset([a, touching]).consistent
    got = estimate_offset([a, apart])
    assert not got.consistent and got.bound_ms == pytest.approx(2.005)


def test_widened_estimate_is_symmetric_and_order_independent() -> None:
    rng = random.Random(3)  # noqa: S311
    rows = [
        ClockExchange(
            10.0 * i, 10.0 * i + 1.0 + (900.0 if i % 3 == 0 else 0.0), 10.0 * i + 2.0
        )
        for i in range(12)
    ]
    first = estimate_offset(rows, resolution_ms=1.0)
    for _ in range(5):
        rng.shuffle(rows)
        again = estimate_offset(rows, resolution_ms=1.0)
        assert (again.offset_ms, again.bound_ms, again.consistent) == (
            first.offset_ms,
            first.bound_ms,
            first.consistent,
        )


def test_one_wild_ping_widens_the_bound_it_does_not_vanish() -> None:
    """A single outlier among 15 agreeing pings is not discarded as noise: the
    safe direction is a wide bound (-> uncertain), never a quietly tight one."""
    rows = [ClockExchange(10.0 * i, 10.0 * i + 1.0, 10.0 * i + 2.0) for i in range(15)]
    rows.append(ClockExchange(500.0, 500.0 + 1.0 + 400.0, 502.0))
    got = estimate_offset(rows, resolution_ms=1.0)
    assert not got.consistent and got.bound_ms > 150.0


# ---------------------------------------------------------- ping dropping


@pytest.mark.parametrize(
    ("pings", "good", "ok"),
    [
        (16, 8, True),
        (16, 7, False),
        (3, 1, True),
        (3, 0, False),
        (1, 1, True),
        (1, 0, False),
        (2, 1, True),
        (15, 7, True),
        (15, 6, False),
    ],
)
def test_half_usable_edge(pings: int, good: int, ok: bool) -> None:
    rows: list[Any] = [_good(i) for i in range(good)] + [None] * (pings - good)
    if ok:
        got, dropped = PERF.ping_clock(_Page(rows), pings=pings)
        assert dropped == pings - good and got.exchanges == good
    else:
        with pytest.raises(ClockOffsetError):
            PERF.ping_clock(_Page(rows), pings=pings)


def test_zero_pings_requested_still_needs_one_usable() -> None:
    with pytest.raises(ClockOffsetError):
        PERF.ping_clock(_Page([]), pings=0)


def test_dropping_cannot_remove_the_disagreeing_half_unless_it_is_unusable() -> None:
    """A step makes two groups; only structurally bad rows are dropped, so
    both groups survive and the result is inconsistent."""
    rows = [_good(i) for i in range(8)] + [_good(i + 8, skew=3000.0) for i in range(8)]
    got, dropped = PERF.ping_clock(_Page(rows), pings=16)
    assert dropped == 0 and not got.consistent


def test_the_ping_that_straddles_a_backwards_step_is_the_only_one_dropped() -> None:
    rows = [_good(i) for i in range(7)]
    rows.append([100.0, 101.0, 99.0])  # the local clock stepped back mid-ping
    behind = [_good(i + 8) for i in range(8)]  # local clock now 5 s behind
    rows += [[g[0] - 5000.0, g[1], g[2] - 5000.0] for g in behind]
    got, dropped = PERF.ping_clock(_Page(rows), pings=16)
    assert dropped == 1
    # the remaining pings straddle the step: the offset moved by 5 s
    assert not got.consistent and got.bound_ms >= 2000.0


@pytest.mark.parametrize(
    "bad", [True, "1", {"a": 1}, [1, 2], [1, 2, 3, 4], 5, [[1, 2, 3]]]
)
def test_hostile_row_shapes_are_dropped_not_fatal(bad: Any) -> None:
    rows: list[Any] = [_good(i) for i in range(12)] + [bad]
    got, dropped = PERF.ping_clock(_Page(rows), pings=16)
    # numeric strings are accepted by float(); everything else is dropped
    assert dropped in (0, 1) and got.exchanges == 12 + (1 - dropped)


def test_rows_none_is_an_error_not_a_traceback() -> None:
    with pytest.raises(ClockOffsetError):
        PERF.ping_clock(_Page(None), pings=16)  # type: ignore[arg-type]


def test_mass_dropping_is_reported_in_pings_dropped() -> None:
    rows: list[Any] = [_good(i) for i in range(8)] + [None] * 8
    got, dropped = PERF.ping_clock(_Page(rows), pings=16)
    block = PERF.phase_clock(got, got, [10.0], dropped=dropped)
    assert block["pings_dropped"] == 8


# --------------------------------------------------------- phase clock


def test_inconsistent_measurement_is_always_a_reason() -> None:
    for side in ("before", "after"):
        bad, good = _clock(consistent=False), _clock()
        block = PERF.phase_clock(
            *((bad, good) if side == "before" else (good, bad)), [1.0]
        )
        assert block["uncertain"] and any("disagree" in r for r in block["reasons"])


def test_a_huge_step_inside_both_measurements_is_uncertain_by_bound_and_flag() -> None:
    wide = estimate_offset(
        [ClockExchange(0.0, 1.0, 2.0), ClockExchange(10.0, 10.0 + 1.0 + 200.0, 12.0)],
        resolution_ms=1.0,
    )
    block = PERF.phase_clock(wide, wide, [1.0])
    assert block["uncertain"] and block["before"]["uncertain"]
    assert block["clock_offset_bound_ms"] > 50.0


def test_move_exactly_at_the_combined_bound_is_allowed_and_beyond_is_not() -> None:
    assert not PERF.phase_clock(_clock(0.0), _clock(3.0), [1.0])["uncertain"]
    assert PERF.phase_clock(_clock(0.0), _clock(3.0001), [1.0])["uncertain"]
    assert PERF.phase_clock(_clock(0.0), _clock(-3.0001), [1.0])["uncertain"]


def test_lag_exactly_minus_bound_is_allowed() -> None:
    assert not PERF.phase_clock(_clock(), _clock(), [-1.5])["uncertain"]
    assert PERF.phase_clock(_clock(), _clock(), [-1.5001])["uncertain"]


def test_both_measurements_failed_is_uncertain_with_two_reasons() -> None:
    block = PERF.phase_clock("boom", "bang", [1.0])
    assert block["uncertain"] and len(block["reasons"]) == 2
    assert block["before"] == {"error": "boom"}


def test_a_failed_after_measurement_alone_makes_the_phase_uncertain() -> None:
    block = PERF.phase_clock(_clock(), "after died", [10.0])
    assert (
        block["uncertain"] and "after: clock measurement failed" in block["reasons"][0]
    )


def test_nan_lag_after_an_impossible_negative_one_is_still_caught() -> None:
    assert PERF.phase_clock(_clock(), _clock(), [-900.0, math.nan])["uncertain"]


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-R3 (minor): min() over lags is order-dependent with a NaN "
    "first, so a NaN lag hides an impossible negative one",
)
def test_nan_lag_does_not_hide_an_impossible_negative_one() -> None:
    assert PERF.phase_clock(_clock(), _clock(), [math.nan, -900.0])["uncertain"]


# ---------------------------------------------------------- JSON written


def _loads_strict(text: str) -> Any:
    def refuse(token: str) -> Any:
        raise ValueError(f"non-standard JSON constant {token}")

    return json.loads(text, parse_constant=refuse)


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-R1: a failed measurement makes clock_offset_bound_ms math.inf "
    "and json.dumps (cli.py) writes the non-standard token Infinity",
)
def test_failed_measurement_block_is_standard_json() -> None:
    block = PERF.phase_clock("ping failed", _clock(), [1.0])
    _loads_strict(json.dumps({"phases": {"a": {"events": {"clock": block}}}}))


def test_failed_measurement_block_still_reads_as_skew_uncertain_whatever_the_encoding() -> (
    None
):
    for bound in (math.inf, None, "inf"):
        clock = PERF.phase_clock("ping failed", _clock(), [1.0])
        clock["clock_offset_bound_ms"] = bound
        phase = {
            "list": {
                k: PERF.summary([1.0])
                for k in (
                    "list_first_render",
                    "list_in_app_nav",
                    "filter_change",
                    "detail_open",
                )
            }
            | {"scroll": {"long_task_max": 0.0}},
            "events": {
                "clock": clock,
                "heartbeat_latency": PERF.summary([5.0]),
                "job_state_latency": PERF.summary([5.0]),
                "job_state_missed": 0,
            },
        }
        assert PERF.verdict({"a": phase}) == "skew_uncertain"


def test_exit_codes_are_exactly_0_1_3() -> None:
    assert PERF.EXIT_CODES == {"pass": 0, "fail": 1, "skew_uncertain": 3}


def test_report_with_all_default_blocks_is_standard_json() -> None:
    block = PERF.phase_clock(_clock(), _clock(), [1.0])
    _loads_strict(json.dumps(block))
