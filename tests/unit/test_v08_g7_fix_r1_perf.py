"""V0.8 G7 fix round 1 (T-8): criterion 2's "<= 2 s live" is checked at p50,
p95 and the slowest event, and a job.state change never shown fails."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mf_ui_perf import load_ui_perf

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _ui_perf() -> ModuleType:
    return load_ui_perf("ui_perf_fix_r1")


PERF = _ui_perf()


def _results(job_state: list[float], heartbeat: list[float], missed: int = 0) -> Any:
    fast = PERF.summary([10.0, 20.0])
    return {
        "list": {
            "list_first_render": fast,
            "list_in_app_nav": fast,
            "filter_change": fast,
            "detail_open": fast,
            "scroll": {"long_task_max": 0.0},
        },
        "events": {
            "heartbeat_latency": PERF.summary(heartbeat),
            "job_state_latency": PERF.summary(job_state),
            "job_state_missed": missed,
            "job_state_missed_examples": ["job_x:running"] if missed else [],
        },
    }


def test_all_fast_passes() -> None:
    assert PERF.check(_results([50.0] * 100, [150.0] * 20)) == []


@pytest.mark.parametrize("event", ["job_state", "heartbeat"])
def test_one_slow_event_fails_on_max(event: str) -> None:
    values = [50.0] * 99 + [2500.0]
    r = (
        _results(values, [150.0] * 20)
        if event == "job_state"
        else _results([50.0] * 20, values)
    )
    problems = PERF.check(r)
    assert any(p.startswith(f"{event}_latency_max") for p in problems), problems


def test_a_slow_median_fails_on_p50() -> None:
    problems = PERF.check(_results([2100.0] * 100, [150.0] * 20))
    names = {p.split(":")[0] for p in problems}
    assert {"job_state_latency_p50", "job_state_latency_p95"} <= names


def test_a_never_shown_state_is_a_failure_not_a_dropped_sample() -> None:
    problems = PERF.check(_results([50.0] * 100, [150.0] * 20, missed=1))
    assert any(p.startswith("job_state_missed: 1") for p in problems), problems


def test_every_live_threshold_is_two_seconds() -> None:
    live = {k: v for k, v in PERF.THRESHOLDS_MS.items() if "_latency_" in k}
    assert set(live) == {
        f"{e}_latency_{s}"
        for e in ("heartbeat", "job_state")
        for s in ("p50", "p95", "max")
    }
    assert set(live.values()) == {2000.0}


T0 = "2026-10-17T10:00:00+00:00"  # epoch ms below are relative to this


def _ms(offset_ms: float) -> float:
    return PERF._parse_at(T0) + offset_ms  # type: ignore[no-any-return]


def _transitions(*states: tuple[str, float]) -> list[dict[str, Any]]:
    from datetime import datetime, timedelta

    base = datetime.fromisoformat(T0)
    return [
        {"to_state": s, "at": (base + timedelta(milliseconds=ms)).isoformat()}
        for s, ms in states
    ]


def test_a_shown_state_is_timed_to_when_it_was_shown() -> None:
    samples = PERF.JobStateSamples()
    samples.add(
        "j",
        _transitions(("queued", 0), ("succeeded", 100)),
        {"queued": _ms(20), "succeeded": _ms(130)},
    )
    assert [round(x) for x in samples.lags] == [20, 30]
    assert [round(x) for x in samples.terminal_lags] == [30]
    assert samples.superseded == 0 and samples.missed == []


def test_a_state_never_shown_is_timed_to_the_next_shown_state() -> None:
    samples = PERF.JobStateSamples()
    samples.add(
        "j",
        _transitions(("submitted", 0), ("planning", 5), ("queued", 10)),
        {"queued": _ms(40)},
    )
    assert [round(x) for x in samples.lags] == [40, 35, 30]
    assert samples.superseded == 2 and samples.missed == []


def test_a_late_superseding_state_is_a_slow_sample_not_hidden() -> None:
    samples = PERF.JobStateSamples()
    samples.add(
        "j", _transitions(("queued", 0), ("running", 10)), {"running": _ms(30_000)}
    )
    assert max(samples.lags) >= 30_000
    r = _results(samples.lags * 20, [150.0] * 20)
    assert any(p.startswith("job_state_latency_max") for p in PERF.check(r))


def test_no_later_state_shown_is_missed_and_terminal_counted() -> None:
    samples = PERF.JobStateSamples()
    samples.add("j", _transitions(("queued", 0), ("succeeded", 10)), {"queued": _ms(5)})
    assert samples.missed == ["j:succeeded"]
    assert samples.missed_terminal == 1


def test_a_state_shown_before_the_change_does_not_count_as_its_display() -> None:
    """`queued` shown at +5 ms (the first time) is not the display of a
    `queued` change at +100 ms after a retry - only a later state can be."""
    samples = PERF.JobStateSamples()
    samples.add(
        "j",
        _transitions(("queued", 0), ("preparing", 50), ("running", 100)),
        {"queued": _ms(5)},
    )
    assert samples.missed == ["j:preparing", "j:running"]
