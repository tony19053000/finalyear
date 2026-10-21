"""Pure helpers over a campaign's run records (V0.9 G5, D218/D220/D246).

Shared by the labels (:mod:`meshforge.controller.telemetry.labels`) and the
data quality report (:mod:`meshforge.bench.quality`) - one definition of each
cost and of "which record counts per repetition", so the two can never
disagree on a median. Moved here from the labels module in G5c (the quality
core lives in ``meshforge.bench``, which may not import the controller); the
labels module re-exports them unchanged.

A *row* is a mapping of run-record column names (docs/TELEMETRY.md) to values.

* :func:`metric_value` - one cost of one record: ``total_ms`` (V0.10 G8: on a
  split job's job row - ``attempt_role`` ``combine`` - the job's
  ``split_job_ms``: lanes, chunks and combine, D271);
  ``compute_per_iter_ns`` = ``compute_ns / compute_iterations`` (D225.3; null
  when either is null or iterations <= 0); ``bytes`` = ``bytes_in +
  bytes_out`` (null when either is null);
* :func:`latest_per_repetition` - one record per (group, config, repetition):
  the latest attempt (a retried run, D234, can leave several ``ok`` records).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "METRICS",
    "MIN_REPETITIONS",
    "latest_per_repetition",
    "metric_value",
]

METRICS: Final = ("total_ms", "compute_per_iter_ns", "bytes")
MIN_REPETITIONS: Final = 3
"""Fewest measured repetitions a configuration needs before a group is labelled
(D216: campaigns repeat >= 3 times) - also the quality report's ``reps_flag``
and criterion 3."""


def metric_value(row: Mapping[str, Any], metric: str) -> float | int | None:
    """One cost of one record (module docstring); None = not measured."""
    if metric == "total_ms":
        if row.get("attempt_role") == "combine":
            # V0.10 G8: a split job's cost is the whole job (lanes + chunks +
            # combine) - the job row's split_job_ms, not the combine's phases
            return _int(row.get("split_job_ms"))
        return _int(row.get("total_ms"))
    if metric == "compute_per_iter_ns":
        ns, iterations = (
            _int(row.get("compute_ns")),
            _int(row.get("compute_iterations")),
        )
        if ns is None or iterations is None or iterations <= 0:
            return None
        return ns / iterations
    if metric == "bytes":
        inn, out = _int(row.get("bytes_in")), _int(row.get("bytes_out"))
        if inn is None or out is None:
            return None
        return inn + out
    raise ValueError(f"unknown metric {metric}")


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _latest_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (row["attempt_created_at"], row["attempt_no"], row["attempt_id"])


def latest_per_repetition(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep one record per (group_id, config_id, repetition): the **latest**
    attempt - created last, then highest attempt number, then highest attempt
    id (deterministic whatever the input order). A config id already names its
    group (it is a digest of it); the group in the key is belt and braces.
    A retried run (D234) can leave several ``ok`` records; the latest is the
    one the campaign kept. Rows
    without a config or repetition are dropped. Sorted by (group_id,
    config_id, repetition)."""
    kept: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for row in rows:
        config, rep = row.get("config_id"), row.get("repetition")
        if not isinstance(config, str) or not isinstance(rep, int):
            continue
        key = (str(row.get("group_id") or ""), config, rep)
        current = kept.get(key)
        if current is None or _latest_key(row) > _latest_key(current):
            kept[key] = row
    return sorted(
        kept.values(),
        key=lambda r: (str(r.get("group_id") or ""), r["config_id"], r["repetition"]),
    )
