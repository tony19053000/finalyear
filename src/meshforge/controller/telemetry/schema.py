"""Telemetry schema v3: the columns of a run record and of a chunk record
(V0.9 G1, D212, ADR-0018; v2 = V0.9 G4, D233.3/D241; v3 = V0.10 G8, D271).

A **run record** is one attempt plus everything known at its start and its end,
written once when the attempt is terminal (:mod:`.recorder`). This module is
the single declaration of its columns - name, type, nullability, meaning - in
the order the Parquet export writes them. Three things must agree with it, and
``tests/unit/controller/test_v09_g1_telemetry_schema.py`` fails otherwise:

* the ``run_record`` table model (:mod:`meshforge.controller.store.models.telemetry`)
  and therefore migrations 0021 + 0023 (``alembic check``);
* the Arrow schema of the export (:mod:`.export`);
* :data:`SCHEMA_FINGERPRINTS`: the digest of these columns for every published
  :data:`SCHEMA_VERSION`. Changing a column without bumping the version - or
  bumping it without recording the new fingerprint - fails the test. Later
  versions (V0.10+) add workload families, not new schemas; when a column must
  change, bump the version and keep reading the old one.

**Versions.** v1 (G1) - the first published shape. v2 (G4, migration 0023)
adds ``result_schema_version`` (the benchmark result version the agent read,
D233.3) and the run's benchmark condition: ``condition`` (its canonical string,
D241) and ``condition_measured`` (what was measured when it was applied and
verified, D217). All three are nullable: a v1 row (written before 0023) keeps
``schema_version`` 1 and nulls there, so one export can hold both - a reader
goes by each row's ``schema_version``.

**v3** (V0.10 G8, migration 0029, D271) - Mode 3:

* the run record gains ``attempt_role`` (``run`` / ``lane`` / ``combine``),
  ``planner_strategy`` + ``planner_node_set`` (the planner's pick as a
  strategy, D215 extended to sets) and the ``split_*`` columns of a split
  job (its first plan, and - on the **job row**, the record of the combine
  attempt that ended the job - its measured makespan, job time, combine time
  and ideal bound); ``strategy`` gains ``mode3``. Every v3 column is
  nullable: a v1/v2 row keeps nulls there (null role = a whole run).
* a second table, the **chunk record** (:data:`CHUNK_COLUMNS`, the dataset's
  ``chunks.parquet``): one row per chunk lease that ended with a result, plus
  the other leases of a committed chunk (a lost speculative copy, an expired
  or released try) - written in the transaction that applied the result
  (checklist 35). Its columns are versioned with the same
  :data:`SCHEMA_VERSION` (:data:`CHUNK_FINGERPRINTS`).

Types (:class:`Kind`): ``text``, ``int64``, ``float64``, ``bool``, ``json``
(JSONB in PostgreSQL; canonical JSON text in Parquet - sorted keys, compact) and
``timestamp`` (UTC, microseconds). Timestamps are the controller's clock and
are for **ordering only** (checklist 28): every duration is a monotonic
measurement taken where the work ran.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ATTEMPT_ROLES",
    "CHUNK_COLUMNS",
    "CHUNK_COLUMN_NAMES",
    "CHUNK_FINGERPRINTS",
    "CHUNK_OUTCOMES",
    "COLUMNS",
    "COLUMN_NAMES",
    "FAILURE_CLASSES",
    "SCHEMA_FINGERPRINTS",
    "SCHEMA_VERSION",
    "STRATEGIES",
    "Column",
    "FailureClass",
    "Kind",
    "fingerprint",
]

SCHEMA_VERSION: Final = 3


class Kind(StrEnum):
    TEXT = "text"
    INT64 = "int64"
    FLOAT64 = "float64"
    BOOL = "bool"
    JSON = "json"
    TIMESTAMP = "timestamp"


class FailureClass(StrEnum):
    """Why a run produced (or did not produce) a usable measurement (D212)."""

    OK = "ok"
    WORKLOAD = "workload"
    """The workload itself failed (crash, self-check failed, OOM, its own limit)."""
    INFRASTRUCTURE = "infrastructure"
    """The fabric failed it (node lost, transfer, env build, disk, internal)."""
    CONDITION_FAILED = "condition_failed"
    """A benchmark condition could not be applied or verified (V0.9 G4, D217)."""
    UNAVAILABLE = "unavailable"
    """The run could not happen here (no CUDA - bench exit 3) or was stopped
    (cancelled) - no measurement, and no fault of the workload."""


FAILURE_CLASSES: Final = tuple(c.value for c in FailureClass)
STRATEGIES: Final = ("local", "mode1", "mode3")
"""V0.9 strategies (D215): the submitter's own node, or Mode 1 on another;
V0.10 G8 (D271): ``mode3`` = a split job run over a node set. A split job run
on one lane ("don't split") is ``local`` / ``mode1`` by its plan."""
ATTEMPT_ROLES: Final = ("run", "lane", "combine")
"""``attempt.role`` (V0.10 D259): a whole run, a split job's lane, its combine."""


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    kind: Kind
    nullable: bool
    doc: str


def _c(name: str, kind: Kind, doc: str, *, nullable: bool = True) -> Column:
    return Column(name, kind, nullable, doc)


_T, _I, _F, _B, _J, _TS = (
    Kind.TEXT,
    Kind.INT64,
    Kind.FLOAT64,
    Kind.BOOL,
    Kind.JSON,
    Kind.TIMESTAMP,
)

COLUMNS: Final[tuple[Column, ...]] = (
    # --- identity ---------------------------------------------------------
    _c("schema_version", _I, "Telemetry schema version of this row.", nullable=False),
    _c("attempt_id", _T, "The attempt (primary key).", nullable=False),
    _c("job_id", _T, "The job the attempt belongs to.", nullable=False),
    _c("attempt_no", _I, "The attempt's fencing token (1 = first).", nullable=False),
    _c(
        "attempt_role",
        _T,
        "run / lane / combine (v3); the combine that ended a split job is the "
        "job row; null = written before v3 (a whole run).",
    ),
    _c("campaign_id", _T, "Benchmark campaign (V0.9 G3); null = not a benchmark."),
    _c("group_id", _T, "Label group: same workload + params + conditions (D218)."),
    _c("config_id", _T, "Campaign configuration (group x strategy)."),
    _c("repetition", _I, "Repetition number within the configuration."),
    _c("fingerprint", _T, "D108 workload fingerprint (64 hex)."),
    # --- placement --------------------------------------------------------
    _c("node_id", _T, "The node that ran the attempt.", nullable=False),
    _c("submitter_node_id", _T, "The submitting laptop's own node, if known."),
    _c(
        "strategy",
        _T,
        "local = ran on the submitter's node; mode1 = elsewhere; mode3 = a "
        "split job run over a node set (v3).",
        nullable=False,
    ),
    _c(
        "placement_mode",
        _T,
        "pinned (forced node) or auto (planner chose).",
        nullable=False,
    ),
    _c("policy", _T, "Placement policy the planner used."),
    _c(
        "planner_node_id",
        _T,
        "Node the planner chose (auto) or prefers unpinned (pinned: D215/D235 "
        "shadow plan, slot occupancy ignored); null = none feasible / older.",
    ),
    _c(
        "planner_estimate_s",
        _F,
        "The planner's estimate for the node used (s); split jobs: for the "
        "candidate of the job's strategy (its predicted makespan).",
    ),
    _c(
        "planner_strategy",
        _T,
        "local / mode1 / mode3 the planner chose (auto) or would have chosen "
        "(pinned / forced, D215) (v3); null = none feasible / older.",
    ),
    _c(
        "planner_node_set",
        _J,
        "mode3 pick: the planner's node set, fastest first (v3); null otherwise.",
    ),
    # --- workload ---------------------------------------------------------
    _c(
        "workload_family",
        _T,
        "Benchmark family (results/workload.json); null = not a benchmark.",
    ),
    _c("workload_params", _J, "Benchmark parameters as the workload reported them."),
    _c("workload_seed", _I, "Benchmark seed."),
    _c("workload_status", _T, "ok / check_failed / unavailable / bad_params."),
    _c("workload_device", _T, "cpu / cuda as the workload reported it."),
    _c(
        "result_schema_version",
        _I,
        "schema_version of the workload's results/workload.json (v2).",
    ),
    _c("p_n", _I, "Flattened parameter n."),
    _c("p_repeats", _I, "Flattened parameter repeats."),
    _c("p_size_mb", _I, "Flattened parameter size_mb."),
    _c("p_size", _I, "Flattened parameter size."),
    _c("p_batch", _I, "Flattened parameter batch."),
    _c("p_width", _I, "Flattened parameter width."),
    _c("p_depth", _I, "Flattened parameter depth."),
    _c("p_seq_len", _I, "Flattened parameter seq_len."),
    _c("p_dtype", _T, "Flattened parameter dtype."),
    _c("env_base", _T, "Manifest env.base (python-cpu / cuda-pytorch)."),
    _c("gpu_request", _T, "Manifest resources.gpu (none / preferred / required)."),
    _c("runtime", _T, "docker / venv / subprocess as the agent reported it."),
    # --- benchmark condition (v2, V0.9 G4, D217/D241) ----------------------
    _c(
        "condition",
        _T,
        "The run's benchmark condition, canonical name[:arg] (none = baseline); "
        "null = not a benchmark / older.",
    ),
    _c(
        "condition_measured",
        _J,
        "What was measured when the condition was applied and verified.",
    ),
    # --- split job (v3, V0.10 G8, D271/D274) --------------------------------
    _c(
        "split_node_set",
        _J,
        "Split jobs: the lanes' nodes of the job's first plan, fastest first.",
    ),
    _c("split_items", _I, "Split jobs: items the job resolved to."),
    _c("split_chunk_items", _I, "Split jobs: items per chunk (first plan)."),
    _c("split_chunks", _I, "Split jobs: chunks (first plan)."),
    _c("split_predicted_s", _F, "Split jobs: the first plan's predicted makespan (s)."),
    _c(
        "split_makespan_ms",
        _I,
        "Job row: first lane RUNNING -> job end (controller clock, D274 C2).",
    ),
    _c(
        "split_job_ms",
        _I,
        "Job row: first lane dispatched -> job end (controller clock): lane "
        "staging, every chunk and the combine - the split job's cost.",
    ),
    _c("split_combine_ms", _I, "Job row: the combine's run time, on its agent."),
    _c(
        "split_ideal_bound_s",
        _F,
        "Job row: items / sum of the lanes' measured items/s + the slowest "
        "lane's stage + env + the combine (D274, from this job's own chunks).",
    ),
    # --- start snapshot (at dispatch) -------------------------------------
    _c("node_state", _T, "Node liveness state at dispatch."),
    _c("node_rtt_ms", _F, "Controller <-> node RTT EWMA at dispatch (ms)."),
    _c("node_cpu_percent", _F, "Node CPU utilisation at dispatch (%)."),
    _c("node_ram_free_bytes", _I, "Node free RAM at dispatch."),
    _c("node_ram_total_bytes", _I, "Node total RAM."),
    _c("node_gpu_util_percent", _F, "Utilisation of the GPU given to the job (%)."),
    _c("node_gpu_vram_free_bytes", _I, "Free VRAM of the GPU given to the job."),
    _c("node_sample_age_s", _F, "Age of the resource sample used (s)."),
    _c(
        "node_live_attempts", _I, "Attempts already live on the node (its queue depth)."
    ),
    _c("node_cpu_score", _F, "Node CPU score (GFLOP/s, all cores)."),
    _c("node_gpu_score", _F, "Node best GPU score (TFLOP/s fp32)."),
    _c("cache_inputs", _I, "Trees the job needed (project + named inputs)."),
    _c("cache_warm_inputs", _I, "Of those, trees the node held whole at dispatch."),
    _c("cache_state", _T, "warm / partial / cold."),
    _c("cache_detail", _J, "Per tree: id, bytes, warm."),
    _c(
        "net_source",
        _T,
        "Whose link the network row is: peer (submitter) or controller.",
    ),
    _c("net_rtt_ms", _F, "Network row to the chosen node: RTT (ms)."),
    _c("net_bw_bytes_s", _F, "Network row to the chosen node: bandwidth (B/s)."),
    _c("net_samples", _I, "Samples behind that network row."),
    # --- phase durations (monotonic, where the work ran) -------------------
    _c("queue_ms", _I, "QUEUED -> PREPARING on the controller; null = skipped."),
    _c("stage_ms", _I, "Agent: fetch + verify + unpack; null = skipped."),
    _c("env_ms", _I, "Agent: environment build/resolve; null = skipped."),
    _c("run_ms", _I, "Agent: workload start to exit; null = skipped."),
    _c(
        "compute_ns",
        _I,
        "The workload's own kernel time, its whole timed loop (benchmarks); "
        "null = none.",
    ),
    _c(
        "compute_iterations",
        _I,
        "Iterations compute_ns covers (one = compute_ns / this); null = not run.",
    ),
    _c("collect_ms", _I, "Agent: exit to terminal report; null = skipped."),
    _c("total_ms", _I, "Sum of the measured phases above (compute excluded)."),
    # --- bytes ------------------------------------------------------------
    _c("bytes_in", _I, "Bytes the node fetched to stage the attempt."),
    _c("bytes_in_cached", _I, "Bytes it already held (cache hits)."),
    _c("bytes_out", _I, "Bytes uploaded: outputs + final logs (DONE only)."),
    # --- outcome ----------------------------------------------------------
    _c("attempt_state", _T, "done / failed / lost / fenced.", nullable=False),
    _c("exit_code", _I, "The workload's exit code."),
    _c("failure_type", _T, "Typed failure (ARCHITECTURE section 13)."),
    _c(
        "failure_class",
        _T,
        "ok / workload / infrastructure / condition_failed / unavailable.",
        nullable=False,
    ),
    # --- clocks -----------------------------------------------------------
    _c("clock_offset_ms", _F, "Node wall clock minus controller's at dispatch (ms)."),
    _c("clock_offset_bound_ms", _F, "The true offset is within +- this (ms)."),
    _c("clock_consistent", _B, "False: the offset exchanges contradicted each other."),
    # --- ordering only (controller clock) ----------------------------------
    _c("attempt_created_at", _TS, "When the attempt was created.", nullable=False),
    _c("attempt_ended_at", _TS, "When the attempt ended (controller clock)."),
    _c("recorded_at", _TS, "When this record was written.", nullable=False),
)

COLUMN_NAMES: Final = tuple(c.name for c in COLUMNS)


def fingerprint(columns: tuple[Column, ...] = COLUMNS) -> str:
    """SHA-256 of the columns' (name, kind, nullable), in order (docs excluded:
    rewording a description is not a schema change)."""
    payload = json.dumps(
        [[c.name, c.kind.value, c.nullable] for c in columns], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


SCHEMA_FINGERPRINTS: Final[dict[int, str]] = {
    1: "69e443de70cbb8bbd25fb04662510910c649b812ad16000ede6d092948df723a",
    2: "6cdab4d3e019c82bf7f1f8d08cfc6b8543a234b642bdc68c1c8f3135d2c02882",
    3: "9411a6f6d8c2fc541d4f9bf834cb34cce5c3eacf7e3efb64daef039934458078",
}
"""Published versions -> their column fingerprint. Never edit a published entry."""

# ------------------------------------------------------------ chunk record

CHUNK_OUTCOMES: Final = (
    "committed",
    "lost",
    "failed",
    "superseded",
    "released",
    "expired",
)
"""How a chunk lease ended: ``committed`` (its result won), ``lost`` (it
finished, another result of the chunk had won), ``failed`` (the chunk failed
there), ``superseded`` (stopped: another copy won first; no result),
``released`` (given back unrun - lane lost or stopping, owner pre-emption) and
``expired`` (its visibility timeout passed)."""

CHUNK_COLUMNS: Final[tuple[Column, ...]] = (
    _c("schema_version", _I, "Telemetry schema version of this row.", nullable=False),
    _c("job_id", _T, "The split job.", nullable=False),
    _c("chunk_index", _I, "The chunk (0-based, in item order).", nullable=False),
    _c(
        "lease_seq",
        _I,
        "The lease's sequence number for this chunk (1 = first try).",
        nullable=False,
    ),
    _c("lane_attempt_id", _T, "The lane attempt that held the lease.", nullable=False),
    _c("node_id", _T, "The lane's node.", nullable=False),
    _c("first_item", _I, "Index of the chunk's first item.", nullable=False),
    _c("items", _I, "Items in the chunk.", nullable=False),
    _c("speculative", _B, "The lease was a speculative copy (D266).", nullable=False),
    _c("won", _B, "This lease's result is the committed one.", nullable=False),
    _c(
        "outcome",
        _T,
        "committed / lost / failed / superseded / released / expired.",
        nullable=False,
    ),
    _c("failure_type", _T, "Typed failure of a failed try."),
    _c(
        "failure_class",
        _T,
        "ok / workload / infrastructure / unavailable (as the run record).",
        nullable=False,
    ),
    _c("exit_code", _I, "The chunk process's exit code."),
    _c("result_digest", _T, "Digest of the chunk's packed output (done tries)."),
    _c("bytes_in", _I, "Bytes the agent placed as the chunk's inputs."),
    _c("bytes_out", _I, "Bytes of the packed output the lane's grant stored."),
    _c("stage_ms", _I, "Agent: the chunk's input staging (monotonic)."),
    _c("env_ms", _I, "Agent: environment work for this chunk (monotonic)."),
    _c("run_ms", _I, "Agent: the chunk process, start to exit (monotonic)."),
    _c("upload_ms", _I, "Agent: pack + upload of the output (monotonic)."),
    _c(
        "leased_at",
        _TS,
        "Lease granted (controller clock, ordering only).",
        nullable=False,
    ),
    _c("ended_at", _TS, "Lease ended (controller clock, ordering only)."),
    _c("recorded_at", _TS, "When this record was written.", nullable=False),
)
"""The chunk record's dataset columns (``chunks.parquet``, D271), in export
order. The table also holds ``stderr_tail`` (the try's redacted stderr tail,
for the API, D261) - operational, never exported."""

CHUNK_COLUMN_NAMES: Final = tuple(c.name for c in CHUNK_COLUMNS)

CHUNK_FINGERPRINTS: Final[dict[int, str]] = {
    3: "080d144e7d8fbd1cf0948004eff2df91dfb53d82db79e20f8f13bf8c1c29fd83",
}
"""Telemetry schema version -> the chunk columns' fingerprint (introduced in
v3). Never edit a published entry; a change bumps :data:`SCHEMA_VERSION`."""
