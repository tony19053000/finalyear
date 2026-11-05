"""``meshforge-agent doctor cpu`` - live CPU readings for a lab check (V1.0 G9-C).

Lab bug #8: during a 12-process burn on Windows the dashboard's Live CPU stayed
near idle. This command lets the owner see, on the laptop itself, what the
agent's sampler reads while a job runs. It reads; it changes nothing:

* **the CPU cap a job gets here** - the owner's ``max_cpu_percent`` (config +
  live ``policy set``), ``[agent.execution] default_cpus``, and the result of
  the same rule the agent plans a run with
  (:func:`~meshforge.agent.execution.plan.job_cpus`), with how it is enforced on
  this OS (Windows: a hard Job Object CPU-rate cap on the whole job; Linux: a
  ``cpu.max`` cgroup only under the systemd unit, else a lowered priority);
* **one line per second** for ``--seconds``: the whole-machine CPU % the agent
  reports (:class:`~meshforge.agent.resources.cpu.CpuMeter`, D435), the
  per-core min / max, and ``old`` - what the pre-G9-C sampler would have said
  (``psutil.cpu_percent(interval=None)`` from a fresh thread each time, as the
  agent's sampling thread did). ``old`` far below ``cpu`` on Windows confirms
  the root cause.

Every source is injectable (:class:`CpuSources`) so tests drive it with fake
counters and a fake clock.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meshforge.agent.execution.plan import job_cpus
from meshforge.agent.process import windows_cpu_rate
from meshforge.agent.resources.cpu import CpuMeter, core_percents
from meshforge.common.owner_policy import OwnerPolicy

__all__ = [
    "CpuCap",
    "CpuSample",
    "CpuSources",
    "cpu_cap",
    "default_threads",
    "render_cap",
    "render_sample",
    "run_samples",
]


@dataclass(frozen=True, slots=True)
class CpuCap:
    """What limits one job's CPU on this node."""

    threads: int
    max_cpu_percent: int | None
    default_cpus: float | None
    asked_cpus: float | None
    cpus: float | None
    """The planned limit in cores; None = no limit."""
    machine_percent: float | None
    """``cpus`` as a share of the whole machine (what the dashboard would show)."""
    enforcement: str

    def as_dict(self) -> dict[str, object]:
        return {
            "threads": self.threads,
            "max_cpu_percent": self.max_cpu_percent,
            "default_cpus": self.default_cpus,
            "asked_cpus": self.asked_cpus,
            "cpus": self.cpus,
            "machine_percent": self.machine_percent,
            "enforcement": self.enforcement,
        }


def _enforcement(platform: str, cpus: float | None) -> str:
    if cpus is None:
        return "none (no CPU limit: the job may use every core)"
    if platform == "win32":
        return (
            "Windows Job Object hard CPU-rate cap on the whole job "
            "(subprocess/venv runtimes; Docker: --cpus)"
        )
    if platform.startswith("linux"):
        return (
            "Linux: cpu.max cgroup when the agent runs under its systemd unit "
            "(Delegate=yes); otherwise a lowered priority only (not a cap); "
            "Docker: --cpus"
        )
    return "not enforced on this OS (Docker: --cpus)"


def cpu_cap(
    policy: OwnerPolicy,
    *,
    threads: int,
    default_cpus: float | None,
    asked_cpus: float | None = None,
    platform: str = sys.platform,
) -> CpuCap:
    """The cap a job asking ``asked_cpus`` (None = no ``resources.cpus``) gets."""
    cpus = job_cpus(asked_cpus, default_cpus, policy, threads)
    share: float | None = None
    if cpus is not None:
        share = (
            windows_cpu_rate(cpus, threads) / 100.0
            if platform == "win32"
            else min(100.0, 100.0 * cpus / max(1, threads))
        )
    return CpuCap(
        threads=threads,
        max_cpu_percent=policy.max_cpu_percent,
        default_cpus=default_cpus,
        asked_cpus=asked_cpus,
        cpus=cpus,
        machine_percent=share,
        enforcement=_enforcement(platform, cpus),
    )


def render_cap(cap: CpuCap, *, policy_source: str) -> list[str]:
    owner = (
        "no limit"
        if cap.max_cpu_percent is None
        else f"{cap.max_cpu_percent}% of {cap.threads} threads"
    )
    asked = "no resources.cpus" if cap.asked_cpus is None else f"cpus={cap.asked_cpus}"
    if cap.cpus is None:
        got = "no CPU cap"
    else:
        got = f"{cap.cpus:.2f} cores = {cap.machine_percent:.1f}% of this machine"
    return [
        f"threads: {cap.threads}",
        f"owner max_cpu_percent: {owner}  ({policy_source})",
        "default_cpus: "
        + ("none" if cap.default_cpus is None else f"{cap.default_cpus}"),
        f"a job with {asked} gets: {got}",
        f"enforced by: {cap.enforcement}",
    ]


@dataclass(frozen=True, slots=True)
class CpuSample:
    t_s: float
    cpu_percent: float | None
    core_min: float | None
    core_max: float | None
    old_percent: float | None
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "t_s": round(self.t_s, 3),
            "cpu_percent": self.cpu_percent,
            "core_min": self.core_min,
            "core_max": self.core_max,
            "old_percent": self.old_percent,
            "note": self.note,
        }


def _psutil() -> Any:
    import psutil  # type: ignore[import-untyped]

    return psutil


def _old_method() -> float | None:
    """``psutil.cpu_percent(interval=None)`` on a fresh thread (pre-G9-C)."""
    box: list[float] = []

    def run() -> None:
        box.append(float(_psutil().cpu_percent(interval=None)))

    thread = threading.Thread(target=run, name="mf-doctor-cpu-old", daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    return box[0] if box else None


@dataclass(slots=True)
class CpuSources:
    """What :func:`run_samples` reads; the defaults are the real machine."""

    cpu_times: Callable[[], Any] = lambda: _psutil().cpu_times()
    per_core_times: Callable[[], list[Any]] = lambda: list(
        _psutil().cpu_times(percpu=True)
    )
    old_percent: Callable[[], float | None] = _old_method
    clock: Callable[[], float] = time.monotonic
    wait: Callable[[float], object] = lambda s: threading.Event().wait(s)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def run_samples(
    seconds: int,
    emit: Callable[[CpuSample], None],
    *,
    interval_s: float = 1.0,
    sources: CpuSources | None = None,
) -> int:
    """Emit one :class:`CpuSample` per ``interval_s`` for ``seconds``; returns
    how many readings had an unknown CPU %. Never raises for a failed read."""
    src = sources if sources is not None else CpuSources()
    meter = CpuMeter(src.cpu_times, clock=src.clock)
    meter.read()
    with contextlib.suppress(Exception):
        src.old_percent()  # its baseline (meaningless first value)
    try:
        cores_before: list[Any] | None = src.per_core_times()
    except Exception:  # noqa: BLE001 - per-core is optional detail
        cores_before = None
    start = src.clock()
    unknown = 0
    count = max(1, round(seconds / interval_s))
    for index in range(count):
        target = start + (index + 1) * interval_s
        delay = target - src.clock()
        if delay > 0:
            src.wait(delay)
        reading = meter.read()
        old: float | None = None
        with contextlib.suppress(Exception):
            old = src.old_percent()
        core_min = core_max = None
        try:
            cores_after = src.per_core_times()
        except Exception:  # noqa: BLE001
            cores_after = None
        if cores_before is not None and cores_after is not None:
            known = [
                p for p in core_percents(cores_before, cores_after) if p is not None
            ]
            if known:
                core_min, core_max = min(known), max(known)
        cores_before = cores_after
        note = ""
        if reading.percent is None:
            unknown += 1
            reason = reading.reason.value if reading.reason is not None else "unknown"
            note = f"cpu unknown: {reason}" + (
                f" {reading.detail}" if reading.detail else ""
            )
        emit(
            CpuSample(
                t_s=src.clock() - start,
                cpu_percent=_rounded(reading.percent),
                core_min=_rounded(core_min),
                core_max=_rounded(core_max),
                old_percent=_rounded(old),
                note=note,
            )
        )
    return unknown


def render_sample(sample: CpuSample) -> str:
    def pct(value: float | None) -> str:
        return "    ?" if value is None else f"{value:5.1f}"

    line = (
        f"t={sample.t_s:6.1f}s  cpu={pct(sample.cpu_percent)}%  "
        f"core min/max={pct(sample.core_min)}/{pct(sample.core_max)}%  "
        f"old={pct(sample.old_percent)}%"
    )
    return f"{line}  ({sample.note})" if sample.note else line


def default_threads() -> int:
    return os.cpu_count() or 1
