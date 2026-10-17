"""Thresholds, statistics, the verdict and the Markdown table (V0.8 G7,
roadmap V0.8 criterion 2, D174; verdict V0.9 G0)."""

from __future__ import annotations

import contextlib
import os
import platform
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from .clocks import bound_uncertain

#: Criterion 2 and the D174 headroom, as asserted by the -m perf test.
#:
#: * Live updates (criterion 2: "<= 2 s"): p50, p95 AND the slowest event, for
#:   heartbeat samples and job.state changes (G7 tester r1 T-8, Lead ruling
#:   D207) - a p95 alone ignores the slowest 5 %, so a few events tens of
#:   seconds late would pass. Every job.state change is a sample: one the page
#:   never showed itself counts with the time until it showed a LATER state of
#:   the job (``job_state_superseded``); one with no later state shown either
#:   is a failure (``job_state_missed``) - never a sample left out.
#: * The list numbers are "responsive" limits chosen by G7 (criterion 2 gives
#:   no number): a user-visible ceiling, not a regression guard - measured
#:   values are 10-30x below them on the dev laptop (progress file, item 4).
#:   Kept as they are by the Lead's ruling; lab step E3 measures the real fabric.
LIVE_LATENCY_MS: Final = 2000.0
THRESHOLDS_MS: Final[dict[str, float]] = {
    "list_first_render_p95": 3000.0,
    "list_in_app_nav_p95": 1000.0,
    "filter_change_p95": 1500.0,
    "detail_open_p95": 1500.0,
    "scroll_long_task_max": 200.0,
    **{
        f"{event}_latency_{stat}": LIVE_LATENCY_MS
        for event in ("heartbeat", "job_state")
        for stat in ("p50", "p95", "max")
    },
}
EXIT_PASS: Final = 0
EXIT_FAIL: Final = 1
EXIT_SKEW_UNCERTAIN: Final = 3


def summary(values: Sequence[float]) -> dict[str, float]:
    """min / p50 / p95 / max (ms) of ``values``; an empty list says so."""
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p95 = (
        statistics.quantiles(ordered, n=20, method="inclusive")[-1]
        if len(ordered) > 1
        else ordered[0]
    )
    return {
        "n": len(ordered),
        "min": round(ordered[0], 1),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(p95, 1),
        "max": round(ordered[-1], 1),
    }


def machine() -> dict[str, Any]:
    """Where the numbers were measured (they mean little without it)."""
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu"] = line.split(":", 1)[1].strip()
                break
    with contextlib.suppress(OSError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                info["ram_gib"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                break
    return info


def check(results: dict[str, Any]) -> list[str]:
    """Each threshold that the results break (empty = all met)."""
    got = {
        "list_first_render_p95": results["list"]["list_first_render"]["p95"],
        "list_in_app_nav_p95": results["list"]["list_in_app_nav"]["p95"],
        "filter_change_p95": results["list"]["filter_change"]["p95"],
        "detail_open_p95": results["list"]["detail_open"]["p95"],
        "scroll_long_task_max": results["list"]["scroll"]["long_task_max"],
    }
    problems: list[str] = []
    if "events" in results:
        events = results["events"]
        for event in ("heartbeat", "job_state"):
            latency = events[f"{event}_latency"]
            if latency.get("n") == 0:
                # Fix round 1 (DEFECT-2): nothing could be timed is a failure
                # the verdict names - never a KeyError, never a pass.
                problems.append(f"{event}_latency: no samples (n=0)")
                continue
            for stat in ("p50", "p95", "max"):
                got[f"{event}_latency_{stat}"] = latency[stat]
        if events["job_state_missed"]:
            problems.append(
                f"job_state_missed: {events['job_state_missed']} job.state "
                f"changes never shown (e.g. {events.get('job_state_missed_examples')})"
            )
    return [
        f"{name}: {value} ms > {THRESHOLDS_MS[name]} ms"
        for name, value in got.items()
        if value > THRESHOLDS_MS[name]
    ] + problems


def skew_uncertain(results: dict[str, Any]) -> bool:
    """Were the live latencies measured on a clock offset that cannot be
    trusted? No offset at all; a phase the measurement marked ``uncertain``
    (:func:`.clocks.phase_clock`); a reported bound that fails
    :func:`.clocks.bound_uncertain` (the same rule on the same number as the
    report's flag); or a latency below ``-bound`` (shown before it happened)."""
    events = results.get("events")
    if events is None:
        return False
    clock = events.get("clock")
    if clock is None or clock.get("uncertain") or clock.get("consistent") is False:
        return True
    try:
        bound = float(clock["clock_offset_bound_ms"])
    except (KeyError, TypeError, ValueError):
        return True
    if bound_uncertain(bound):
        return True
    for event in ("heartbeat", "job_state"):
        latency = events.get(f"{event}_latency") or {}
        if latency.get("n") and float(latency["min"]) < -bound:
            return True
    return False


def verdict(phases: dict[str, dict[str, Any]]) -> str:
    """``pass``, ``fail`` or ``skew_uncertain`` for a whole run.

    A broken list limit (no clock involved) fails whatever the clock. With an
    uncertain clock offset in any phase, the live-latency numbers are neither
    a pass nor a fail: ``skew_uncertain``. Otherwise any broken limit fails.
    """
    if any(check({"list": phase["list"]}) for phase in phases.values()):
        return "fail"
    if any(skew_uncertain(phase) for phase in phases.values()):
        return "skew_uncertain"
    return "fail" if any(check(phase) for phase in phases.values()) else "pass"


EXIT_CODES: Final = {
    "pass": EXIT_PASS,
    "fail": EXIT_FAIL,
    "skew_uncertain": EXIT_SKEW_UNCERTAIN,
}


def table(phases: dict[str, dict[str, Any]]) -> str:
    """The results as a Markdown table (one column per phase)."""
    rows = [
        ("jobs list first render (cold load), ms", "list", "list_first_render"),
        ("jobs list via in-app nav, ms", "list", "list_in_app_nav"),
        ("load next 50 rows by scrolling, ms", "list", "load_more_by_scroll"),
        ("filter change -> rows shown, ms", "list", "filter_change"),
        ("job page open, ms", "list", "detail_open"),
        ("heartbeat -> DOM, ms", "events", "heartbeat_latency"),
        ("job.state -> DOM, ms", "events", "job_state_latency"),
    ]
    names = list(phases)
    lines = [
        "| measure (p50 / p95 / max) | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for label, group, key in rows:
        cells = []
        for name in names:
            s = phases[name].get(group, {}).get(key)
            cells.append(
                "-"
                if not s or not s.get("n")
                else f"{s['p50']} / {s['p95']} / {s['max']}"
            )
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    scroll_cells = []
    for name in names:
        sc = phases[name]["list"]["scroll"]
        scroll_cells.append(
            f"max long task {sc['long_task_max']} ms ({sc['long_tasks']} tasks), "
            f"{phases[name]['list']['rows_loaded']} rows loaded, "
            f"<= {sc['max_dom_rows']} in the DOM"
        )
    lines.append("| scroll through the list | " + " | ".join(scroll_cells) + " |")
    return "\n".join(lines)
