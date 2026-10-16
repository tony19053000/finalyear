"""V0.8 G7 fix round 1 (T-8): criterion 2's "<= 2 s live" is checked at p50,
p95 and the slowest event, and a job.state change never shown fails."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _ui_perf() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ui_perf_fix_r1", ROOT / "scripts" / "ui_perf.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
