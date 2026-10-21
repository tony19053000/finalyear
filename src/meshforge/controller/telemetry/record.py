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

**Params** (G4 item 0.9, D240): ``workload_params`` keeps only the keys of the
family's own params model (D236) whose value is a **scalar** (text, integer,
float, bool, null); a list or an object is dropped, and a non-finite float
(NaN, +-inf - JSONB and the canonical export JSON refuse both) becomes null.
``condition_measured`` (v2) is a JSON object whose non-finite floats become null
the same way.

**Schema v2** (G4, D233.3/D241): ``result_schema_version`` is the result
version the agent read from ``results/workload.json`` (>= 1, else null);
``condition`` / ``condition_measured`` have one writer,
:func:`~.conditions.condition_outcome` (the run's campaign identity plus the
agent's condition report). ``condition`` is kept only when it is a canonical
``name[:arg]`` (:func:`meshforge.common.bench_conditions.parse_condition`
round-trips it) within :data:`CONDITION_CHARS`, else null; ``condition_measured``
goes through the same non-finite-float and storability guards as every JSON
column, so a NaN the agent reported is null, never a refused row.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.bench.workloads import UnknownFamilyError, get_family
from meshforge.common.bench_conditions import ConditionError, parse_condition
from meshforge.common.errors import FailureType
from meshforge.common.states import AttemptState
from meshforge.controller.planner.inputs import scores_of
from meshforge.controller.telemetry.conditions import (
    ConditionOutcome,
    condition_outcome,
)
from meshforge.controller.telemetry.schema import (
    ATTEMPT_ROLES,
    COLUMN_NAMES,
    SCHEMA_VERSION,
    FailureClass,
)
from meshforge.controller.telemetry.split_facts import SplitFacts

__all__ = [
    "BENCH_EXIT_CHECK_FAILED",
    "BENCH_EXIT_UNAVAILABLE",
    "CONDITION_CHARS",
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
CONDITION_CHARS: Final = 128
"""Width of ``condition`` (``VARCHAR(128)`` in migration 0023): a canonical
condition string such as ``netem:delay=50ms,loss=1%``."""


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
    repetition; V0.9 G4 adds condition, session_label, condition_facts;
    V0.10 G8 adds the run's family, params and seed."""
    role: str | None = None
    """V0.10 G8: ``attempt.role`` (run / lane / combine)."""
    split: SplitFacts | None = None
    """V0.10 G8: the split job's facts (:mod:`.split_facts`); None = the job
    runs whole."""


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
    # V0.10 G8: a campaign's mode3 run is forced onto a node set (D271)
    pinned = bool(source.placement.get("node_id")) or bool(
        source.placement.get("node_set")
    )
    facts = source.split
    workload = source.workload if facts is None else _split_workload(source, facts)
    condition = condition_outcome(
        campaign=source.campaign, workload=workload, state=source.state
    )
    decision = _decision_of(source)
    planner_node = decision.shadow if pinned else decision.chosen
    planner_strategy, planner_set = _planner_pick(source, decision, pinned)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": source.attempt_id,
        "job_id": source.job_id,
        "attempt_no": int(source.attempt_no),
        "attempt_role": source.role if source.role in ATTEMPT_ROLES else None,
        **_campaign(source.campaign),
        "fingerprint": source.fingerprint,
        "node_id": source.node_id,
        "submitter_node_id": source.submitter_node_id,
        "strategy": _strategy(source, facts),
        "placement_mode": "pinned" if pinned else "auto",
        "policy": decision.policy or source.job_policy,
        # D215: what the planner chose (auto) or would have chosen (pinned)
        "planner_node_id": planner_node,
        "planner_estimate_s": facts.predicted_s
        if facts is not None
        else _estimate_for(decision.candidates, source.node_id),
        "planner_strategy": planner_strategy,
        "planner_node_set": planner_set,
        **_split_columns(source.attempt_id, facts),
        **_workload(workload),
        **_manifest(source.manifest),
        "runtime": _short(source.runtime),
        # V0.9 G4 (D217/D241): one writer, condition_outcome; A's guards
        "condition": _canonical_condition(condition.condition),
        "condition_measured": None
        if condition.measured is None
        else _finite_json(condition.measured),
        **_start(snapshot, telemetry),
        **_phases(source.phases, workload),
        **_bytes(source.transfers, source.output_bytes),
        "attempt_state": source.state,
        "exit_code": source.exit_code,
        "failure_type": source.failure_type,
        "failure_class": _class(source, workload, condition).value,
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


def _canonical_condition(value: object) -> str | None:
    """``value`` when it is a canonical ``name[:arg]`` condition that fits
    :data:`CONDITION_CHARS`, else None (blank, unparsable or non-canonical
    text is never stored as a condition)."""
    text = _bounded(value, CONDITION_CHARS)
    if text is None:
        return None
    try:
        return text if parse_condition(text).canonical() == text else None
    except ConditionError:
        return None


def _class(
    source: RecordSource,
    workload: Mapping[str, Any] | None,
    condition: ConditionOutcome,
) -> FailureClass:
    """:func:`failure_class`, except that an unverified benchmark condition
    makes the record ``condition_failed`` (D217: never labelled)."""
    if condition.failed:
        return FailureClass.CONDITION_FAILED
    return failure_class(source.state, source.failure_type, source.exit_code, workload)


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
    return _bounded(value, SHORT_TEXT_CHARS)


def _bounded(value: object, chars: int) -> str | None:
    """A non-empty string of at most ``chars`` characters, else None."""
    text = _text(value)
    return text if text is not None and len(text) <= chars else None


def _scalar(value: object) -> tuple[bool, object]:
    """``(keep, value)`` for one params value: scalars are kept (a non-finite
    float as None), lists / objects / anything else are not."""
    if value is None or isinstance(value, str | bool | int):
        return True, value
    if isinstance(value, float):
        return True, value if math.isfinite(value) else None
    return False, None


def _finite_json(value: object, depth: int = 0) -> object:
    """``value`` with every non-finite float replaced by None (lists and
    objects copied; anything deeper than :data:`MAX_JSON_DEPTH` is left for
    :func:`_storable` to refuse)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= MAX_JSON_DEPTH:
        return value
    if isinstance(value, Mapping):
        return {k: _finite_json(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite_json(v, depth + 1) for v in value]
    return value


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


def _known_params(family: object, params: object) -> dict[str, Any] | None:
    """The reported params restricted to the keys of the family's own params
    model (tester r1 TR1-A2, D236): a key the family does not define - an
    agent bug, or a value smuggled into the report - is never stored. Only
    scalar values are kept, a non-finite float as None (D240, module
    docstring). An unknown family keeps none (None)."""
    if not isinstance(params, Mapping) or not isinstance(family, str):
        return None
    try:
        fields = get_family(family).params_model.model_fields
    except UnknownFamilyError:
        return None
    kept: dict[str, Any] = {}
    for key, value in params.items():
        if not isinstance(key, str) or key not in fields:
            continue
        keep, scalar = _scalar(value)
        if keep:
            kept[key] = scalar
    return kept


def _workload(workload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = workload or {}
    params = _known_params(data.get("family"), data.get("params"))
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
        "result_schema_version": _result_version(data.get("result_schema_version")),
        **flat,
    }


def _result_version(value: object) -> int | None:
    version = _int(value)
    return version if version is not None and version >= 1 else None


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


@dataclass(frozen=True, slots=True)
class _Decision:
    policy: str | None
    chosen: str | None
    candidates: Sequence[Mapping[str, Any]]
    shadow: str | None
    shadow_set: tuple[str, ...]


def _decision_of(source: RecordSource) -> _Decision:
    """The plan decision a record is built from: the attempt's own (a whole
    run), or - V0.10 G8 - the split job's first dispatched decision for every
    one of its records (lanes after the first and the combine have none of
    their own)."""
    facts = source.split
    if facts is not None:
        return _Decision(
            facts.decision_policy,
            facts.decision_chosen,
            facts.decision_candidates,
            facts.decision_shadow,
            facts.decision_shadow_set,
        )
    return _Decision(
        source.decision_policy,
        source.decision_chosen,
        source.decision_candidates,
        source.decision_shadow,
        (),
    )


def _strategy(source: RecordSource, facts: SplitFacts | None) -> str:
    """A whole run: ``local`` on the submitter's node, else ``mode1``. A
    split job: its first plan's (``mode3`` when it split; "don't split" is
    ``local`` / ``mode1`` by its lane), for every one of its records."""
    if facts is not None and facts.strategy is not None:
        return facts.strategy
    local = (
        source.submitter_node_id is not None
        and source.submitter_node_id == source.node_id
    )
    return "local" if local else "mode1"


def _planner_pick(
    source: RecordSource, decision: _Decision, pinned: bool
) -> tuple[str | None, list[str] | None]:
    """(planner_strategy, planner_node_set): what the planner chose (auto)
    or would have chosen (pinned / forced: the shadow plan, D215)."""
    facts = source.split
    if facts is not None and not pinned:
        strategy = facts.strategy
        return strategy, list(facts.node_set) if strategy == "mode3" else None
    node = decision.shadow if pinned else decision.chosen
    if node is None:
        return None, None
    if decision.shadow_set and pinned:
        return "mode3", list(decision.shadow_set)
    local = source.submitter_node_id is not None and node == source.submitter_node_id
    return ("local" if local else "mode1"), None


def _split_columns(attempt_id: str, facts: SplitFacts | None) -> dict[str, Any]:
    times = None if facts is None else facts.times.get(attempt_id)
    return {
        "split_node_set": None
        if facts is None or not facts.node_set
        else list(facts.node_set),
        "split_items": None if facts is None else facts.items,
        "split_chunk_items": None if facts is None else facts.chunk_items,
        "split_chunks": None if facts is None else facts.chunks,
        "split_predicted_s": None if facts is None else facts.predicted_s,
        "split_makespan_ms": None if times is None else times.makespan_ms,
        "split_job_ms": None if times is None else times.job_ms,
        "split_combine_ms": None if times is None else times.combine_ms,
        "split_ideal_bound_s": None if times is None else times.ideal_bound_s,
    }


def _split_workload(
    source: RecordSource, facts: SplitFacts
) -> Mapping[str, Any] | None:
    """A split benchmark job's workload identity (V0.10 G8): its chunks write
    no ``workload.json``, so family / params / seed come from the campaign
    run that made the job (the controller's own rows). ``status`` is ``ok``
    on the **job row** only - the combine that ended the job DONE, which
    every chunk's own self-check passed (a failed check fails the chunk,
    then the job)."""
    campaign = source.campaign or {}
    family = campaign.get("family")
    if not isinstance(family, str):
        return source.workload
    return {
        "family": family,
        "params": campaign.get("params"),
        "seed": campaign.get("seed"),
        "status": "ok" if source.attempt_id in facts.times else None,
    }


def _estimate_for(
    candidates: Sequence[Mapping[str, Any]], node_id: str
) -> float | None:
    """The estimate of ``node_id``'s whole-run row (a split row - mode
    ``split`` - is a set, never "the node used"; D280)."""
    for candidate in candidates:
        if candidate.get("node_id") != node_id or candidate.get("mode") == "split":
            continue
        estimate = _mapping(candidate.get("estimate"))
        return _float(estimate.get("total_s"))
    return None


def canonical_json(value: object) -> str:
    """Sorted keys, compact, no NaN: the Parquet form of a ``json`` column."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
