"""V0.9 G0 tester round 1: try to break the skew-safe live-latency measure
(D210 item 1, D213), from the brief criteria, not from the implementation.

Tests marked ``xfail(strict=True)`` with ``DEFECT-n`` pin a defect found in
round 1: they must start passing (and lose the marker) when it is fixed.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from collections.abc import Callable
from types import ModuleType
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


def _perf() -> ModuleType:
    return load_ui_perf("ui_perf_v09_g0_tester_r1")


PERF = _perf()


def _clock(offset: float = 0.0, bound: float = 1.0) -> ClockOffset:
    return ClockOffset(offset_ms=offset, bound_ms=bound, rtt_ms=2 * bound, exchanges=16)


# ------------------------------------------------------------------ estimator


@pytest.mark.parametrize("skew", [0.0, 5_000.0, -5_000.0, 3.6e6, -3.6e6, 1e12, -1e12])
def test_huge_skew_and_asymmetric_paths_stay_inside_the_bound(skew: float) -> None:
    rng = random.Random(int(abs(skew) % 1e6) + 7)  # noqa: S311
    base = 1.76e12
    for _ in range(300):
        out, back = rng.uniform(0.0, 80.0), rng.uniform(0.0, 80.0)
        sent = base + rng.uniform(0, 1e6)
        ex = ClockExchange(sent, sent + out + skew, sent + out + back)
        got = estimate_offset([ex])
        assert abs(got.offset_ms - skew) <= got.bound_ms + 1e-3 * max(
            1.0, abs(skew) * 1e-9
        )


def test_fully_one_sided_path_error_equals_the_bound_never_more() -> None:
    # all of the delay on the way out (remote stamps at the very end)
    ex = ClockExchange(0.0, 100.0, 100.0)
    got = estimate_offset([ex])
    assert abs(got.offset_ms - 0.0) <= got.bound_ms + 1e-9
    # all on the way back (remote stamps at the very start)
    ex = ClockExchange(0.0, 0.0, 100.0)
    got = estimate_offset([ex])
    assert abs(got.offset_ms - 0.0) <= got.bound_ms + 1e-9


def test_zero_rtt_is_allowed_and_exact() -> None:
    got = estimate_offset([ClockExchange(10.0, 15.0, 10.0)])
    assert got.offset_ms == 5.0 and got.bound_ms == 0.0


def test_ties_pick_a_deterministic_exchange() -> None:
    a = ClockExchange(0.0, 10.0, 2.0)
    b = ClockExchange(100.0, 130.0, 102.0)
    assert estimate_offset([a, b]) == estimate_offset([a, b])
    assert estimate_offset([a, b]).exchanges == 2


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("slot", [0, 1, 2])
def test_non_finite_values_in_any_slot_are_refused(bad: float, slot: int) -> None:
    vals = [0.0, 1.0, 2.0]
    vals[slot] = bad
    with pytest.raises(ClockOffsetError):
        estimate_offset([ClockExchange(*vals)])


def test_one_bad_exchange_among_good_ones_is_not_silently_dropped() -> None:
    good = ClockExchange(0.0, 1.0, 2.0)
    stepped = ClockExchange(10.0, 11.0, 4.0)  # local clock stepped back
    with pytest.raises(ClockOffsetError):
        estimate_offset([good, stepped])


def test_a_clock_step_between_pings_is_not_reported_as_a_tight_offset() -> None:
    before = [ClockExchange(t, t + 1.0, t + 2.0) for t in (0.0, 10.0, 20.0)]
    # the browser/controller clock stepped by 5 s after the third ping
    after = [ClockExchange(t, t + 1.0 + 5000.0, t + 2.0) for t in (30.0, 40.0)]
    got = estimate_offset(before + after)
    # Honest outcomes: refuse, or widen the bound to cover the disagreement.
    assert got.bound_ms >= 2500.0


# ------------------------------------------------- measure_clock on a fake page


class _Page:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows
        self.asked: list[Any] = []

    def evaluate(self, script: str, arg: Any = None) -> list[list[Any]]:
        self.asked.append((script, arg))
        return self.rows


def test_measure_clock_uses_date_now_resolution_and_server_time_in_seconds() -> None:
    # remote clock 5 s ahead, rtt 4 ms, server_time in SECONDS as the API sends
    rows = [[1000.0, 1002.0 + 5000.0, 1004.0]]
    page = _Page(rows)
    # the real page returns server_time*1000 already; emulate: values are ms
    got = PERF.measure_clock(page, pings=1)
    assert page.asked[0][1] == 1
    assert abs(got.offset_ms - 5000.0) <= got.bound_ms
    assert got.bound_ms == pytest.approx((4.0 + 1.0) / 2)


def test_measure_clock_with_no_rows_raises_a_clean_error() -> None:
    with pytest.raises(ClockOffsetError):
        PERF.measure_clock(_Page([]))


def test_measure_clock_with_a_backwards_ping_raises_not_clamps() -> None:
    with pytest.raises(ClockOffsetError):
        PERF.measure_clock(_Page([[10.0, 11.0, 4.0]]))


def test_measure_clock_rows_with_json_nulls_are_refused() -> None:
    with pytest.raises((ClockOffsetError, TypeError, ValueError)):
        PERF.measure_clock(_Page([[1.0, None, 2.0]]))


# ------------------------------------------------------------------- verdict


def _phase(bound: float | None, live_ms: float = 100.0) -> dict[str, Any]:
    stat = {"n": 5, "min": 1.0, "p50": live_ms, "p95": live_ms, "max": live_ms}
    fast = {"n": 5, "min": 1.0, "p50": 1.0, "p95": 1.0, "max": 1.0}
    events: dict[str, Any] = {
        "heartbeat_latency": stat,
        "job_state_latency": stat,
        "job_state_missed": 0,
    }
    if bound is not None:
        events["clock"] = PERF.clock_report(_clock(0.0, bound))
    return {
        "list": {
            "list_first_render": fast,
            "list_in_app_nav": fast,
            "filter_change": fast,
            "detail_open": fast,
            "scroll": {"long_task_max": 0.0},
        },
        "events": events,
    }


def test_bound_exactly_at_the_limit_is_certain_and_just_over_is_not() -> None:
    assert PERF.verdict({"a": _phase(50.0)}) == "pass"
    assert PERF.verdict({"a": _phase(50.1)}) == "skew_uncertain"


def test_the_flag_and_the_verdict_agree_just_over_the_limit() -> None:
    phase = _phase(50.04)
    flag = phase["events"]["clock"]["uncertain"]
    assert flag is True
    assert PERF.verdict({"a": phase}) == "skew_uncertain"


def test_the_uncertain_flag_in_the_report_matches_the_verdict() -> None:
    for bound in (49.9, 50.0, 50.1, 1e6):
        phase = _phase(bound)
        flag = phase["events"]["clock"]["uncertain"]
        assert (PERF.verdict({"a": phase}) == "skew_uncertain") is flag


def test_an_infinite_bound_never_counts_as_a_certain_pass() -> None:
    for bound in (math.inf,):
        phase = _phase(1.0)
        phase["events"]["clock"]["clock_offset_bound_ms"] = bound
        assert PERF.verdict({"a": phase}) != "pass"


def test_one_uncertain_phase_among_certain_ones_taints_the_run() -> None:
    assert (
        PERF.verdict({"a": _phase(1.0), "b": _phase(90.0), "c": _phase(1.0)})
        == "skew_uncertain"
    )


def test_exit_codes_are_distinct_and_cover_every_verdict() -> None:
    codes = PERF.EXIT_CODES
    assert set(codes) == {"pass", "fail", "skew_uncertain"}
    assert sorted(codes.values()) == [0, 1, 3]


def test_a_failed_list_limit_and_a_loose_clock_is_a_fail_exit_1() -> None:
    bad = _phase(500.0)
    bad["list"]["list_first_render"]["p95"] = 99999.0
    assert PERF.EXIT_CODES[PERF.verdict({"a": bad})] == 1


def test_a_phase_where_nothing_could_be_timed_gets_a_verdict_not_a_traceback() -> None:
    phase = _phase(1.0)
    phase["events"]["heartbeat_latency"] = PERF.summary([])  # {"n": 0}
    assert PERF.verdict({"a": phase}) in {"fail", "skew_uncertain"}  # never pass


# --------------------------------------------------- frames / unmatched / revisit


def _iso_at(base_ms: float) -> Callable[[float], str]:
    """ISO time of ``base_ms + ms`` (typed stand-in for the original lambdas)."""

    def at(ms: float) -> str:
        return dt.datetime.fromtimestamp((base_ms + ms) / 1000, dt.UTC).isoformat()

    return at


def _frame(data: Any, type_: str = "node.metrics") -> str:
    return json.dumps({"type": type_, "data": data})


class _NoPage:
    def on(self, *_: Any) -> None:
        pass


def _frames() -> Any:
    return PERF.MetricsFrames(_NoPage(), "nod_A")


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00\xff\xfe",
        "not json",
        "[]",
        "null",
        "7",
        '"node.metrics"',
        json.dumps({"type": "node.metrics"}),
        json.dumps({"type": "node.metrics", "data": "x"}),
        json.dumps({"type": "node.metrics", "data": None}),
        json.dumps({"type": "node.metrics", "data": [1]}),
        _frame({"node_id": "nod_A", "observed_at": "t", "recv_at": "abc"}),
        _frame({"node_id": "nod_A", "observed_at": "t", "recv_at": [1]}),
        _frame({"node_id": "nod_A", "recv_at": 1.0}),  # no observed_at
    ],
)
def test_hostile_frames_never_raise(payload: Any) -> None:
    f = _frames()
    f.frame(payload)  # must not raise
    assert f.received == {}


def test_other_nodes_and_other_types_are_ignored_not_counted_unreadable() -> None:
    f = _frames()
    f.frame(_frame({"node_id": "nod_B", "observed_at": "t", "recv_at": 1.0}))
    f.frame(
        _frame({"node_id": "nod_A", "observed_at": "t", "recv_at": 1.0}, "job.state")
    )
    assert f.received == {} and f.unreadable == 0


def test_recv_at_null_counts_unreadable() -> None:
    f = _frames()
    f.frame(_frame({"node_id": "nod_A", "observed_at": "t", "recv_at": None}))
    assert f.unreadable == 1 and f.received == {}


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_recv_at_is_unreadable_not_a_latency(bad: str) -> None:
    f = _frames()
    f.frame(
        '{"type":"node.metrics","data":{"node_id":"nod_A",'
        f'"observed_at":"t","recv_at":{bad}}}}}'
    )
    assert f.received == {} and f.unreadable == 1


def test_same_observed_at_twice_keeps_the_latest_arrival_deterministically() -> None:
    f = _frames()
    for r in (1.0, 2.0):
        f.frame(_frame({"node_id": "nod_A", "observed_at": "t", "recv_at": r}))
    assert f.received == {"t": 2.0}


def test_heartbeat_lag_is_negative_when_the_page_precedes_the_clock_bound() -> None:
    # documents behaviour: lags are signed; the sign is how clock trouble shows
    got = PERF.HeartbeatSamples.build(
        [{"at": "t", "shown": 1000.0}], {"t": 2.0}, _clock(0.0, 1.0)
    )
    assert got.lags == [-1000.0]


def test_impossible_negative_lags_make_the_phase_uncertain() -> None:
    phase = _phase(1.0)
    phase["events"]["heartbeat_latency"] = PERF.summary([-5000.0] * 10)
    assert PERF.verdict({"a": phase}) != "pass"


def test_job_state_clock_jump_mid_run_shifts_every_sample_equally() -> None:
    # transitions on a controller clock 1 h ahead; mapped through the offset
    clock = _clock(3.6e6, 1.0)
    at = _iso_at(1.76e12)
    s = PERF.JobStateSamples()
    s.add(
        "job_1",
        [
            {"to_state": "queued", "at": at(3.6e6)},
            {"to_state": "running", "at": at(3.6e6 + 1000)},
        ],
        {"queued": 1.76e12 + 120, "running": 1.76e12 + 1150},
        clock,
    )
    assert [round(x) for x in s.lags] == [120, 150]


def test_retry_does_not_pollute_latency_and_first_visits_still_count() -> None:
    at = _iso_at(0.0)
    s = PERF.JobStateSamples()
    s.add(
        "job_r",
        [
            {"to_state": "queued", "at": at(1000)},
            {"to_state": "running", "at": at(2000)},
            {"to_state": "queued", "at": at(3000)},  # retry re-entry
            {"to_state": "running", "at": at(4000)},  # retry re-entry
            {"to_state": "succeeded", "at": at(5000)},
        ],
        {"queued": 1100.0, "running": 2100.0, "succeeded": 5100.0},
    )
    assert s.lags == [100.0, 100.0, 100.0]
    assert (s.revisited, s.retried_jobs, len(s.missed)) == (2, 1, 0)


def test_a_retry_after_a_stall_cannot_make_a_late_terminal_pass() -> None:
    at = _iso_at(0.0)
    s = PERF.JobStateSamples()
    s.add(
        "job_r",
        [
            {"to_state": "queued", "at": at(0)},
            {"to_state": "running", "at": at(1000)},
            {"to_state": "queued", "at": at(2000)},
            {"to_state": "running", "at": at(3000)},
            {"to_state": "succeeded", "at": at(4000)},
        ],
        {"queued": 50.0, "running": 1050.0, "succeeded": 34_000.0},  # 30 s late
    )
    assert max(s.lags) == 30_000.0  # the terminal state's lateness is kept


def test_job_with_no_transitions_or_unshown_everything_is_missed_not_crash() -> None:
    at = _iso_at(0.0)
    s = PERF.JobStateSamples()
    s.add("job_e", [], {})
    s.add("job_m", [{"to_state": "queued", "at": at(0)}], {})
    assert s.missed == ["job_m:queued"]


def test_unparsable_transition_time_raises_loudly_not_silently() -> None:
    s = PERF.JobStateSamples()
    with pytest.raises(ValueError):
        s.add("j", [{"to_state": "queued", "at": "garbage"}], {"queued": 1.0})
