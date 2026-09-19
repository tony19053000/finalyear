"""Warmup + repetition runner with typed, recorded failures.

A spike's measurement function is called ``warmup + reps`` times. Warmup samples
are kept in the result (honesty) but never enter the summary. A failed repetition
raises :class:`RepError`; it is recorded with its ``failure_type`` and the run
continues, unless the error is ``fatal`` (e.g. the server vanished), which stops
this case at once so a dead peer costs one timeout, not ``reps`` timeouts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from spikes._harness.stats import Summary, summarize

#: A JSON-compatible value (kept loose on purpose, like ``_inventory.model.JSON``).
type JSON = Any
#: One repetition's measurements; metric values are numbers, extras any JSON.
type Sample = dict[str, JSON]

PHASE_WARMUP: Final = "warmup"
PHASE_MEASURE: Final = "measure"


class RepError(Exception):
    """A repetition failed. ``failure_type`` is a short snake_case category."""

    def __init__(self, failure_type: str, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.fatal = fatal


@dataclass(slots=True)
class CaseResult:
    """All repetitions of one case (e.g. ``tcp-upload-x1``) and their summary.

    ``metrics`` maps metric name -> unit; only those keys of each sample are
    summarised. ``attrs`` describes the case (direction, peers, ...).
    """

    case_id: str
    label: str
    metrics: dict[str, str]
    attrs: dict[str, JSON] = field(default_factory=dict)
    requested_reps: int = 0
    requested_warmup: int = 0
    samples: list[Sample] = field(default_factory=list)
    warmup: list[Sample] = field(default_factory=list)
    errors: list[dict[str, JSON]] = field(default_factory=list)
    aborted: bool = False

    def summary(self) -> dict[str, Summary]:
        return {
            name: summarize(s[name] for s in self.samples if _is_number(s.get(name)))
            for name in sorted(self.metrics)
        }

    def to_json(self) -> dict[str, JSON]:
        return {
            "label": self.label,
            "attrs": self.attrs,
            "metrics": self.metrics,
            "requested_reps": self.requested_reps,
            "requested_warmup": self.requested_warmup,
            "samples": self.samples,
            "warmup": self.warmup,
            "errors": self.errors,
            "aborted": self.aborted,
            "summary": {k: v.to_json() for k, v in self.summary().items()},
        }


def _is_number(value: JSON) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def error_record(rep: int, phase: str, exc: RepError) -> dict[str, JSON]:
    return {
        "rep": rep,
        "phase": phase,
        "failure_type": exc.failure_type,
        "message": str(exc),
        "fatal": exc.fatal,
    }


def run_reps(
    case_id: str,
    measure: Callable[[int], Sample],
    *,
    reps: int,
    warmup: int,
    metrics: Mapping[str, str],
    label: str = "",
    attrs: Mapping[str, JSON] | None = None,
    pause_s: float = 0.0,
    on_rep: Callable[[str, int, Sample | RepError], None] | None = None,
) -> CaseResult:
    """Run ``warmup`` then ``reps`` repetitions of ``measure(index)``.

    ``index`` counts from 0 over warmup and measured reps together. Only
    :class:`RepError` is caught; any other exception is a bug and propagates.
    ``pause_s`` sleeps between repetitions (lets TCP state / queues drain).
    """
    if reps < 1:
        raise ValueError("reps must be >= 1")
    if warmup < 0:
        raise ValueError("warmup must be >= 0")
    result = CaseResult(
        case_id=case_id,
        label=label or case_id,
        metrics=dict(metrics),
        attrs=dict(attrs or {}),
        requested_reps=reps,
        requested_warmup=warmup,
    )
    for index in range(warmup + reps):
        phase = PHASE_WARMUP if index < warmup else PHASE_MEASURE
        if index and pause_s > 0:
            time.sleep(pause_s)
        try:
            sample = measure(index)
        except RepError as exc:
            result.errors.append(error_record(index, phase, exc))
            if on_rep is not None:
                on_rep(phase, index, exc)
            if exc.fatal:
                result.aborted = True
                break
            continue
        (result.warmup if phase == PHASE_WARMUP else result.samples).append(sample)
        if on_rep is not None:
            on_rep(phase, index, sample)
    return result
