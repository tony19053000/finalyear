"""Build one run record from what the database holds (V0.9 G1, D212). Pure.

:func:`build_record` turns a :class:`RecordSource` - the terminal attempt, its
job, its phase rows, its plan decision and its transfer rows, all already
persisted - into the column -> value mapping of :mod:`.schema`. No I/O, no clock
(``recorded_at`` is the database's ``now()``), so the recorder can re-run it at
any time and a test can feed it a real attempt's rows.

Everything "at start" comes from the attempt's **start snapshot** (written at
dispatch, in the dispatch transaction: node load, RAM, GPU, queue depth, cache
warm/cold per tree, the network row to the chosen node, scores, clock offset) -
never from the current state of those tables, which has moved on by the time
the record is built.

**Failure class** (:func:`failure_class`, D212): ``done`` -> ``ok``; a
benchmark (a workload report is present) that exited 3 or reported
``unavailable`` -> ``unavailable``, exit 4 / ``check_failed`` / ``bad_params``
-> ``workload``; otherwise by the typed failure: the workload's own failures
(:data:`WORKLOAD_FAILURES`) -> ``workload``, ``cancelled`` -> ``unavailable``
(stopped, no measurement), everything else (node lost, fenced, transfer, env,
disk, internal ...) -> ``infrastructure``. Every record has a class (checklist
29).

**Untrusted values.** The workload report (and the job manifest) carry values a
user chose. Every text column is bounded to its column width
(:data:`SHORT_TEXT_CHARS`) - a longer value is null, never truncated into a
different value - and no text or JSON value with a NUL or a lone surrogate
(PostgreSQL refuses both), or JSON nested deeper than :data:`MAX_JSON_DEPTH`, is
kept: it is null. So a hostile report cannot make PostgreSQL refuse the record
(the recorder still isolates a refused record, :mod:`.recorder`).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.common.errors import FailureType
from meshforge.common.states import AttemptState
from meshforge.controller.planner.inputs import scores_of
from meshforge.controller.telemetry.schema import (
    COLUMN_NAMES,
    SCHEMA_VERSION,
    FailureClass,
)

__all__ = [
    "BENCH_EXIT_CHECK_FAILED",
    "BENCH_EXIT_UNAVAILABLE",
    "FLAT_PARAMS",
    "MAX_JSON_DEPTH",
    "SHORT_TEXT_CHARS",
    "WORKLOAD_FAILURES",
    "PhaseValue",
    "RecordSource",
    "TransferRow",
    "build_record",
    "failure_class",
]

BENCH_EXIT_UNAVAILABLE: Final = 3
BENCH_EXIT_CHECK_FAILED: Final = 4
WORKLOAD_FAILURES: Final = frozenset(
    {
        FailureType.USER_ERROR,
        FailureType.OOM,
        FailureType.VRAM_OOM,
        FailureType.DEADLINE_EXCEEDED,
        FailureType.INVALID_INPUT,
    }
)
"""Typed failures that are the workload's own doing (its code, its memory, its
own time limit, its own bad request)."""

FLAT_PARAMS: Final = (
    ("p_n", "n", int),
    ("p_repeats", "repeats", int),
    ("p_size_mb", "size_mb", int),
    ("p_size", "size", int),
    ("p_batch", "batch", int),
    ("p_width", "width", int),
    ("p_depth", "depth", int),
    ("p_seq_len", "seq_len", int),
    ("p_dtype", "dtype", str),
)
"""Known parameters copied into their own columns (the JSON keeps them all)."""

_INT64_MAX: Final = 2**63 - 1
SHORT_TEXT_CHARS: Final = 64
"""Width of the record's short text columns (``VARCHAR(64)`` in migration 0021)."""
MAX_JSON_DEPTH: Final = 32
"""Deepest JSON value a record keeps (benchmark params are flat)."""


@dataclass(frozen=True, slots=True)
class PhaseValue:
    duration_ms: int
    skipped: bool


@dataclass(frozen=True, slots=True)
class TransferRow:
    source: str
    """``cache``, ``controller`` or a peer's node id."""
    bytes: int


@dataclass(frozen=True, slots=True)
class RecordSource:
    """The persisted rows one record is built from (see module docstring)."""

    attempt_id: str
    job_id: str
    attempt_no: int
    node_id: str
    state: str
    created_at: dt.datetime
    ended_at: dt.datetime | None = None
    failure_type: str | None = None
    exit_code: int | None = None
    runtime: str | None = None
    output_bytes: int | None = None
    start_snapshot: Mapping[str, Any] | None = None
    workload: Mapping[str, Any] | None = None
    """The agent's workload report (attempt.workload), benchmarks only."""
    manifest: Mapping[str, Any] = field(default_factory=dict)
    placement: Mapping[str, Any] = field(default_factory=dict)
    submitter_node_id: str | None = None
    job_policy: str | None = None
    fingerprint: str | None = None
    phases: Mapping[str, PhaseValue] = field(default_factory=dict)
    decision_policy: str | None = None
    decision_chosen: str | None = None
    decision_candidates: Sequence[Mapping[str, Any]] = ()
    decision_shadow: str | None = None
    """V0.9 G3 (D215): for a pinned job, the node the planner's unpinned
    shadow plan chose (``plan_decision.shadow_chosen_node_id``)."""
    transfers: Sequence[TransferRow] = ()
    campaign: Mapping[str, Any] | None = None
    """Campaign identity (V0.9 G3 fills it): campaign_id, group_id, config_id,
    repetition."""


def failure_class(
    state: str,
    failure_type: str | None,
    exit_status: int | None,
    workload: Mapping[str, Any] | None,
) -> FailureClass:
    """See the module docstring."""
    if state == AttemptState.DONE.value:
        return FailureClass.OK
    if workload is not None:
        status = workload.get("status")
        # (exit statuses are small numbers, not credentials)
        if exit_status == BENCH_EXIT_UNAVAILABLE or status == "unavailable":
            return FailureClass.UNAVAILABLE
        if exit_status == BENCH_EXIT_CHECK_FAILED or status in (
            "check_failed",
            "bad_params",
        ):
            return FailureClass.WORKLOAD
    try:
        typed = FailureType(failure_type) if failure_type else None
    except ValueError:
        typed = None
    if typed in WORKLOAD_FAILURES:
        return FailureClass.WORKLOAD
    if typed is FailureType.CANCELLED:
        return FailureClass.UNAVAILABLE
    return FailureClass.INFRASTRUCTURE


def build_record(source: RecordSource) -> dict[str, Any]:
    """The record of ``source`` - exactly :data:`~.schema.COLUMN_NAMES` minus
    ``recorded_at`` (the database stamps it)."""
    snapshot = source.start_snapshot or {}
    telemetry = _mapping(snapshot.get("telemetry"))
    pinned = bool(source.placement.get("node_id"))
    workload = source.workload
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": source.attempt_id,
        "job_id": source.job_id,
        "attempt_no": int(source.attempt_no),
        **_campaign(source.campaign),
        "fingerprint": source.fingerprint,
        "node_id": source.node_id,
        "submitter_node_id": source.submitter_node_id,
        "strategy": "local"
        if source.submitter_node_id is not None
        and source.submitter_node_id == source.node_id
        else "mode1",
        "placement_mode": "pinned" if pinned else "auto",
        "policy": source.decision_policy or source.job_policy,
        # D215: what the planner chose (auto) or would have chosen (pinned)
        "planner_node_id": source.decision_shadow if pinned else source.decision_chosen,
        "planner_estimate_s": _estimate_for(source.decision_candidates, source.node_id),
        **_workload(workload),
        **_manifest(source.manifest),
        "runtime": _short(source.runtime),
        **_start(snapshot, telemetry),
        **_phases(source.phases, workload),
        **_bytes(source.transfers, source.output_bytes),
        "attempt_state": source.state,
        "exit_code": source.exit_code,
        "failure_type": source.failure_type,
        "failure_class": failure_class(
            source.state, source.failure_type, source.exit_code, workload
        ).value,
        **_clock(telemetry),
        "attempt_created_at": source.created_at,
        "attempt_ended_at": source.ended_at,
    }
    missing = set(COLUMN_NAMES) - set(record) - {"recorded_at"}
    extra = set(record) - set(COLUMN_NAMES)
    if missing or extra:  # pragma: no cover - the schema test holds this
        raise AssertionError(f"record columns drifted: {missing=} {extra=}")
    return {name: _storable_or_none(value) for name, value in record.items()}


# ------------------------------------------------------------------ helpers


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -_INT64_MAX - 1 <= value <= _INT64_MAX else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _short(value: object) -> str | None:
    """A non-empty string that fits a short column, else None (never cut)."""
    text = _text(value)
    return text if text is not None and len(text) <= SHORT_TEXT_CHARS else None


def _storable_text(text: str) -> bool:
    if "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate
        return False
    return True


def _storable(value: object) -> bool:
    """``value`` (text or JSON) can be stored in PostgreSQL text/JSONB as is:
    no key or string with a NUL or a lone surrogate, no key that is not a
    string, at most :data:`MAX_JSON_DEPTH` deep. Iterative."""
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            if not _storable_text(item):
                return False
        elif isinstance(item, Mapping | list | tuple):
            if depth >= MAX_JSON_DEPTH:
                return False
            if isinstance(item, Mapping):
                for key in item:
                    if not isinstance(key, str) or not _storable_text(key):
                        return False
                children = list(item.values())
            else:
                children = list(item)
            stack.extend((child, depth + 1) for child in children)
    return True


def _storable_or_none(value: object) -> object:
    if isinstance(value, str | Mapping | list | tuple) and not _storable(value):
        return None
    return value


def _campaign(campaign: Mapping[str, Any] | None) -> dict[str, Any]:
    data = campaign or {}
    return {
        "campaign_id": _short(data.get("campaign_id")),
        "group_id": _short(data.get("group_id")),
        "config_id": _short(data.get("config_id")),
        "repetition": _int(data.get("repetition")),
    }


def _workload(workload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = workload or {}
    params = data.get("params")
    params = dict(params) if isinstance(params, Mapping) else None
    flat: dict[str, Any] = {}
    for column, key, kind in FLAT_PARAMS:
        value = None if params is None else params.get(key)
        flat[column] = _int(value) if kind is int else _short(value)
    return {
        "workload_family": _short(data.get("family")),
        "workload_params": params,
        "workload_seed": _int(data.get("seed")),
        "workload_status": _short(data.get("status")),
        "workload_device": _short(data.get("device")),
        **flat,
    }


def _manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    env = _mapping(manifest.get("env"))
    resources = _mapping(manifest.get("resources"))
    return {
        "env_base": _short(env.get("base")),
        "gpu_request": _short(resources.get("gpu")),
    }


def _start(snapshot: Mapping[str, Any], telemetry: Mapping[str, Any]) -> dict[str, Any]:
    node = _mapping(snapshot.get("node"))
    resources = _mapping(snapshot.get("resources"))
    cpu_score, gpu_score = scores_of(_mapping(snapshot.get("scores")))
    used = _int(resources.get("ram_used_bytes"))
    total = _int(resources.get("ram_total_bytes"))
    gpu = _gpu(resources, snapshot.get("gpu_indices"))
    cache = telemetry.get("cache")
    trees = (
        [t for t in cache if isinstance(t, Mapping)]
        if isinstance(cache, list)
        else None
    )
    warm = None if trees is None else sum(1 for t in trees if t.get("warm") is True)
    network = _mapping(telemetry.get("network"))
    return {
        "node_state": _short(node.get("state")),
        "node_rtt_ms": _float(node.get("rtt_ms")),
        "node_cpu_percent": _float(resources.get("cpu_percent")),
        "node_ram_free_bytes": None
        if used is None or total is None
        else max(0, total - used),
        "node_ram_total_bytes": total,
        "node_gpu_util_percent": None
        if gpu is None
        else _float(gpu.get("util_percent")),
        "node_gpu_vram_free_bytes": None if gpu is None else _vram_free(gpu),
        "node_sample_age_s": _float(resources.get("age_s")),
        "node_live_attempts": _int(telemetry.get("node_live_attempts")),
        "node_cpu_score": cpu_score,
        "node_gpu_score": gpu_score,
        "cache_inputs": None if trees is None else len(trees),
        "cache_warm_inputs": warm,
        "cache_state": _cache_state(trees, warm),
        "cache_detail": None
        if trees is None
        else [
            {
                "tree_id": _text(t.get("tree_id")),
                "bytes": _int(t.get("bytes")),
                "warm": t.get("warm") is True,
            }
            for t in trees
        ],
        "net_source": _short(network.get("source")),
        "net_rtt_ms": _float(network.get("rtt_ms")),
        "net_bw_bytes_s": _float(network.get("bw_bytes_s")),
        "net_samples": _int(network.get("samples")),
    }


def _gpu(resources: Mapping[str, Any], indices: object) -> Mapping[str, Any] | None:
    """The sample of the GPU the job was given (the first of ``gpu_indices``)."""
    gpus = resources.get("gpus")
    if not isinstance(gpus, list) or not isinstance(indices, list) or not indices:
        return None
    for gpu in gpus:
        if isinstance(gpu, Mapping) and gpu.get("index") == indices[0]:
            return gpu
    return None


def _vram_free(gpu: Mapping[str, Any]) -> int | None:
    used, total = _int(gpu.get("vram_used_bytes")), _int(gpu.get("vram_total_bytes"))
    return None if used is None or total is None else max(0, total - used)


def _cache_state(trees: list[Mapping[str, Any]] | None, warm: int | None) -> str | None:
    if trees is None or warm is None or not trees:
        return None
    if warm == len(trees):
        return "warm"
    return "cold" if warm == 0 else "partial"


def _phases(
    phases: Mapping[str, PhaseValue], workload: Mapping[str, Any] | None
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    total = 0
    measured = False
    for name in ("queue", "stage", "env", "run", "collect"):
        value = phases.get(name)
        if value is None or value.skipped:
            out[f"{name}_ms"] = None
            continue
        out[f"{name}_ms"] = int(value.duration_ms)
        total += int(value.duration_ms)
        measured = True
    out["total_ms"] = total if measured else None
    compute = None if workload is None else _int(workload.get("compute_ns"))
    out["compute_ns"] = compute if compute is None or compute >= 0 else None
    iterations = None if workload is None else _int(workload.get("compute_iterations"))
    # D228: the number of iterations compute_ns covers; null when nothing ran
    out["compute_iterations"] = (
        iterations
        if out["compute_ns"] is not None and iterations is not None and iterations >= 1
        else None
    )
    return out


def _bytes(
    transfers: Sequence[TransferRow], output_bytes: int | None
) -> dict[str, Any]:
    fetched = sum(t.bytes for t in transfers if t.source != "cache")
    cached = sum(t.bytes for t in transfers if t.source == "cache")
    return {
        "bytes_in": fetched if transfers else None,
        "bytes_in_cached": cached if transfers else None,
        "bytes_out": _int(output_bytes),
    }


def _clock(telemetry: Mapping[str, Any]) -> dict[str, Any]:
    clock = _mapping(telemetry.get("clock"))
    consistent = clock.get("consistent")
    return {
        "clock_offset_ms": _float(clock.get("clock_offset_ms")),
        "clock_offset_bound_ms": _float(clock.get("clock_offset_bound_ms")),
        "clock_consistent": consistent if isinstance(consistent, bool) else None,
    }


def _estimate_for(
    candidates: Sequence[Mapping[str, Any]], node_id: str
) -> float | None:
    for candidate in candidates:
        if candidate.get("node_id") != node_id:
            continue
        estimate = _mapping(candidate.get("estimate"))
        return _float(estimate.get("total_s"))
    return None


def canonical_json(value: object) -> str:
    """Sorted keys, compact, no NaN: the Parquet form of a ``json`` column."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
