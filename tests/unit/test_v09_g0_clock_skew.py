"""V0.9 G0 item 1 (D210 item 1, D213): the dashboard's live-latency numbers
are skew-safe.

* ``meshforge.common.clock_offset``: NTP-style offset from round trips, the
  minimum-RTT exchange wins, bound = RTT/2, unusable exchanges refused;
* ``ui_perf``: a heartbeat is timed from its arrival at the controller
  (``recv_at``), so an agent clock 5 s off changes nothing; controller
  timestamps are moved onto the browser's clock with the measured offset; a
  bound over 50 ms is ``skew_uncertain``, never a pass;
* the controller stamps ``recv_at`` (its own wall clock at arrival) on every
  ``node.metrics`` payload, whatever the agent's ``observed_at`` says.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
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

ROOT = Path(__file__).resolve().parents[2]


def _ui_perf() -> ModuleType:
    return load_ui_perf("ui_perf_v09_g0")


PERF = _ui_perf()
T0 = 1_760_000_000_000.0  # ms, controller epoch


# ---------------------------------------------------------------- the estimator


def _exchange(
    local_sent: float, one_way_out: float, one_way_back: float, skew: float
) -> ClockExchange:
    """A round trip where the remote clock = local clock + ``skew``."""
    remote = local_sent + one_way_out + skew
    return ClockExchange(local_sent, remote, local_sent + one_way_out + one_way_back)


def test_symmetric_round_trip_recovers_the_offset_exactly() -> None:
    clock = estimate_offset([_exchange(1000.0, 4.0, 4.0, skew=-3000.0)])
    assert clock.offset_ms == pytest.approx(-3000.0)
    assert clock.bound_ms == pytest.approx(4.0)
    assert clock.rtt_ms == pytest.approx(8.0)
    assert clock.exchanges == 1


def test_asymmetric_path_stays_inside_the_bound() -> None:
    for out, back in ((1.0, 39.0), (39.0, 1.0), (20.0, 20.0)):
        clock = estimate_offset([_exchange(0.0, out, back, skew=1234.0)])
        assert abs(clock.offset_ms - 1234.0) <= clock.bound_ms + 1e-9


def test_the_shortest_round_trip_wins() -> None:
    clock = estimate_offset(
        [
            _exchange(0.0, 200.0, 5.0, skew=77.0),  # slow, asymmetric
            _exchange(500.0, 2.0, 2.0, skew=77.0),  # the best
            _exchange(900.0, 30.0, 30.0, skew=77.0),
        ]
    )
    assert clock.rtt_ms == pytest.approx(4.0)
    assert clock.offset_ms == pytest.approx(77.0)
    assert clock.exchanges == 3


def test_remote_to_local_conversion() -> None:
    clock = ClockOffset(offset_ms=2500.0, bound_ms=1.0, rtt_ms=2.0, exchanges=1)
    assert clock.to_local_ms(T0) == T0 - 2500.0
    assert clock.as_dict() == {
        "clock_offset_ms": 2500.0,
        "clock_offset_bound_ms": 1.0,
        "rtt_ms": 2.0,
        "exchanges": 1,
    }


@pytest.mark.parametrize(
    "exchanges",
    [
        [],
        [ClockExchange(10.0, 5.0, 9.0)],  # received before sent: the clock stepped
        [ClockExchange(0.0, math.nan, 1.0)],
        [ClockExchange(0.0, 1.0, math.inf)],
        [ClockExchange(0.0, 1.0, 2.0), ClockExchange(10.0, 5.0, 9.0)],
    ],
)
def test_unusable_exchanges_are_refused_never_clamped(
    exchanges: list[ClockExchange],
) -> None:
    with pytest.raises(ClockOffsetError):
        estimate_offset(exchanges)


# ---------------------------------------------------------------- heartbeats


def _iso(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.UTC).isoformat()


def _heartbeats(
    agent_skew_ms: float, browser_behind_ms: float, display_ms: float = 150.0
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """10 samples arriving at the controller each 1 s; the page shows each
    ``display_ms`` later. ``observed_at`` is stamped by an agent whose clock is
    ``agent_skew_ms`` off; the browser's clock is ``browser_behind_ms`` behind
    the controller's."""
    shown: list[dict[str, Any]] = []
    received: dict[str, float] = {}
    for i in range(10):
        arrived = T0 + i * 1000.0  # controller clock
        observed = _iso(arrived - 40.0 + agent_skew_ms)  # agent clock
        received[observed] = arrived / 1000.0
        shown.append(
            {"at": observed, "shown": arrived + display_ms - browser_behind_ms}
        )
    return shown, received


def _clock(browser_behind_ms: float, rtt_ms: float = 2.0) -> ClockOffset:
    """What the ping exchange measures: controller - browser = +behind."""
    return estimate_offset(
        [_exchange(5_000.0, rtt_ms / 2, rtt_ms / 2, skew=browser_behind_ms)]
    )


@pytest.mark.parametrize("agent_skew_ms", [5_000.0, -5_000.0, 3_600_000.0])
def test_an_agent_clock_offset_leaves_the_heartbeat_latency_unchanged(
    agent_skew_ms: float,
) -> None:
    clock = _clock(0.0)
    base = PERF.HeartbeatSamples.build(*_heartbeats(0.0, 0.0), clock)
    skewed = PERF.HeartbeatSamples.build(*_heartbeats(agent_skew_ms, 0.0), clock)
    assert skewed.lags == base.lags
    assert skewed.unmatched == base.unmatched == 0
    assert base.lags == pytest.approx([150.0] * 10)


def test_the_old_measure_was_skewed_by_the_agent_clock() -> None:
    """Regression guard for D210 item 1: browser receive minus the agent's
    observed_at adds the agent's skew (the measure this replaces)."""
    shown, _ = _heartbeats(5_000.0, 0.0)
    old = [
        c["shown"] - dt.datetime.fromisoformat(c["at"]).timestamp() * 1000
        for c in shown
    ]
    assert min(old) < -4_000.0  # 5 s skew made every sample look "early"


def test_a_browser_behind_the_controller_is_corrected_by_the_offset() -> None:
    behind = 2_750.0
    samples = PERF.HeartbeatSamples.build(*_heartbeats(5_000.0, behind), _clock(behind))
    assert samples.lags == pytest.approx([150.0] * 10)
    uncorrected = PERF.HeartbeatSamples.build(*_heartbeats(0.0, behind), _clock(0.0))
    assert uncorrected.lags == pytest.approx([150.0 - behind] * 10)


def test_a_change_without_a_frame_is_unmatched_not_a_sample() -> None:
    shown, received = _heartbeats(0.0, 0.0)
    del received[shown[3]["at"]]
    samples = PERF.HeartbeatSamples.build(shown, received, _clock(0.0))
    assert samples.unmatched == 1 and len(samples.lags) == 9


class _FakePage:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


class _FakeSocket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


def test_metrics_frames_keep_only_this_nodes_metrics_from_the_event_socket() -> None:
    page = _FakePage()
    frames = PERF.MetricsFrames(page, "node_A")
    events_ws = _FakeSocket("wss://h:1/v1/events/ws")
    logs_ws = _FakeSocket("wss://h:1/v1/jobs/job_1/logs/ws")
    page.handlers["websocket"](events_ws)
    page.handlers["websocket"](logs_ws)
    assert "framereceived" in events_ws.handlers
    assert logs_ws.handlers == {}
    feed = events_ws.handlers["framereceived"]
    sample = {"node_id": "node_A", "observed_at": "2026-10-17T10:00:00+00:00"}
    feed(json.dumps({"type": "hello", "seq": 0}))
    feed(json.dumps({"type": "node.metrics", "data": {**sample, "recv_at": 1.5}}))
    feed(
        json.dumps(
            {
                "type": "node.metrics",
                "data": {**sample, "node_id": "node_B", "recv_at": 9.0},
            }
        )
    )
    feed(json.dumps({"type": "job.state", "data": {"job_id": "j"}}))
    feed(json.dumps({"type": "node.metrics", "data": {**sample, "recv_at": None}}))
    feed("not json")
    feed(json.dumps({"type": "node.metrics"}))
    assert frames.received == {sample["observed_at"]: 1.5}
    assert frames.unreadable == 3


# ---------------------------------------------------------------- job.state


def _tr(*steps: tuple[str, float]) -> list[dict[str, Any]]:
    return [{"to_state": s, "at": _iso(T0 + at)} for s, at in steps]


def test_job_state_controller_times_are_moved_onto_the_browser_clock() -> None:
    behind = 1_800.0  # the browser's clock is 1.8 s behind the controller's
    transitions = _tr(("queued", 0.0), ("running", 500.0), ("succeeded", 900.0))
    shown = {
        "queued": T0 + 100.0 - behind,
        "running": T0 + 600.0 - behind,
        "succeeded": T0 + 1000.0 - behind,
    }
    with_clock = PERF.JobStateSamples()
    with_clock.add("j", transitions, shown, _clock(behind))
    assert with_clock.lags == pytest.approx([100.0] * 3)
    same_host = PERF.JobStateSamples()
    same_host.add("j", transitions, shown)  # no clock: one host (old behaviour)
    assert same_host.lags == pytest.approx([100.0 - behind] * 3)


# ---------------------------------------------------------------- verdict


def _phase(
    bound_ms: float | None, *, list_ms: float = 10.0, live_ms: float = 100.0
) -> dict[str, Any]:
    fast = PERF.summary([list_ms])
    live = PERF.summary([live_ms])
    events: dict[str, Any] = {
        "heartbeat_latency": live,
        "job_state_latency": live,
        "job_state_missed": 0,
    }
    if bound_ms is not None:
        events["clock"] = PERF.clock_report(
            ClockOffset(
                offset_ms=0.0, bound_ms=bound_ms, rtt_ms=2 * bound_ms, exchanges=16
            )
        )
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


def test_a_tight_clock_passes() -> None:
    assert (
        PERF.verdict({"a": _phase(0.5), "b": _phase(PERF.MAX_CLOCK_BOUND_MS)}) == "pass"
    )


def test_a_loose_clock_is_skew_uncertain_not_a_pass() -> None:
    phases = {"a": _phase(0.5), "b": _phase(PERF.MAX_CLOCK_BOUND_MS + 0.1)}
    assert PERF.verdict(phases) == "skew_uncertain"
    assert PERF.skew_uncertain(phases["b"]) and not PERF.skew_uncertain(phases["a"])
    assert phases["b"]["events"]["clock"]["uncertain"] is True
    assert PERF.EXIT_CODES["skew_uncertain"] == 3


def test_a_loose_clock_does_not_hide_a_live_latency_failure_as_a_pass() -> None:
    assert PERF.verdict({"a": _phase(80.0, live_ms=5000.0)}) == "skew_uncertain"


def test_no_measured_clock_is_uncertain() -> None:
    assert PERF.verdict({"a": _phase(None)}) == "skew_uncertain"


def test_a_list_limit_fails_whatever_the_clock() -> None:
    assert PERF.verdict({"a": _phase(500.0, list_ms=1e9)}) == "fail"


def test_a_live_limit_fails_with_a_tight_clock() -> None:
    assert PERF.verdict({"a": _phase(1.0, live_ms=2500.0)}) == "fail"


def test_a_list_only_phase_needs_no_clock() -> None:
    phase = _phase(None)
    del phase["events"]
    assert PERF.verdict({"bulk": phase}) == "pass"


# ---------------------------------------------------------------- controller


def test_the_controller_stamps_recv_at_with_its_own_clock_not_the_agents() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.common.proto.meshforge.v1 import agent_pb2
    from meshforge.controller.events.watcher import metrics_frame
    from meshforge.controller.resources import live_sample

    controller_now = dt.datetime(2026, 10, 17, 12, 0, 0, 250_000, tzinfo=dt.UTC)
    update = agent_pb2.ResourceUpdate(sequence=1, cpu_percent=5.0)
    update.observed_at.FromDatetime(controller_now + dt.timedelta(seconds=5))
    sample = live_sample(
        update, session_epoch=1, received_at=10.0, wall_clock=lambda: controller_now
    )
    frame = metrics_frame("node_A", sample)
    assert frame["recv_at"] == round(controller_now.timestamp(), 3)
    assert (
        frame["observed_at"] == (controller_now + dt.timedelta(seconds=5)).isoformat()
    )


def test_a_sample_without_observed_at_arrives_and_is_observed_at_the_same_moment() -> (
    None
):
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.common.proto.meshforge.v1 import agent_pb2
    from meshforge.controller.resources import live_sample

    now = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.UTC)
    sample = live_sample(
        agent_pb2.ResourceUpdate(sequence=1),
        session_epoch=1,
        received_at=1.0,
        wall_clock=lambda: now,
    )
    assert sample.observed_at == sample.received_wall == now


def test_the_attempt_start_snapshot_of_a_real_sample_is_json() -> None:
    """Regression (found by the -m perf e2e run): the start snapshot is a JSON
    column; ``received_wall`` (a datetime) must not leak into it, or every
    dispatch fails with StatementError and no job ever starts."""
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.common.proto.meshforge.v1 import agent_pb2
    from meshforge.controller.jobs.spec import start_snapshot
    from meshforge.controller.resources import live_sample

    now = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.UTC)
    update = agent_pb2.ResourceUpdate(sequence=1, cpu_percent=5.0)
    update.observed_at.FromDatetime(now)
    sample = live_sample(
        update, session_epoch=1, received_at=10.0, wall_clock=lambda: now
    )
    snapshot = start_snapshot(
        hardware={},
        hardware_scores=None,
        node_state="online",
        rtt_ms=1.0,
        sample=sample,
        monotonic_now=12.0,
        wall_now=now,
        gpu_indices=(),
    )
    json.dumps(snapshot)  # raises TypeError on a datetime
    assert "received_wall" not in snapshot["resources"]
    assert snapshot["resources"]["age_s"] == 2.0
