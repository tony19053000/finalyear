"""The ML dataset's schema: versions, features and learnable terms (V0.15 G1,
D333-D335). Pure: no database, no clock, no logging.

**Versions.** :data:`FEATURE_VERSION` names the feature list below (its order,
names, units and meaning); :data:`DATASET_VERSION` names the dataset layout
(id columns, target columns, Parquet metadata). Either changes -> bump it; a
fingerprint test pins both (:func:`schema_fingerprint`).

**Features** (D334) come from the decision's **replay snapshot** - what the
planner saw - never from today's database, so there is no train/serve skew.
Each has a ``group``; G5's ablations drop one group at a time:

* ``hardware`` - measured scores and sizes of the candidate node (never model
  names; so a model can generalise to a laptop it has not seen);
* ``load`` - its live load at decision time;
* ``cache`` - what it already holds of the job;
* ``network`` - the links the estimator could use;
* ``job_size`` - the job's own size and declared resources;
* ``analytical`` - the analytical model's raw prediction, its source and the
  history behind it;
* ``family`` - the workload family (a category; optional ablation feature
  only - a held-out family is unseen by construction).

A missing input is ``NaN``, **never 0** (D334): 0 bytes free and "unknown" are
different facts. Flags (``0.0`` / ``1.0``) are used only where the planner's
own view is binary (a node with no GPU has 0 GPUs; an env the history does not
name is, to the planner, not built).

**Terms** (D333). Whole job (Local / Mode 1, attempt role ``run``):
``transfer``, ``env``, ``compute``, ``output`` - each the measured phase the
calibration maps it to (:data:`~meshforge.controller.planner.calibration.
TERM_PHASE`, imported, never copied). Mode 3 (a split job): per lane
``lane_stage`` (the lane's stage = the job's trees minus the items, D264) and
``lane_env``, measured by that lane attempt's ``stage`` / ``env`` phases;
``chunk_rate`` per lane - stored as **seconds per item** (``1 / rate``) so the
residual keeps the same sign and the same ``raw x exp(y)`` correction as every
other term: predicted = the lane's planned item rate (``rate_items_s`` of the
node's one-lane row), actual = the summed ``run_ms`` of that lane's committed
chunks over their summed items (``chunk_record``); and ``combine`` - the
combine attempt's ``stage + env + run`` phases (its ``collect`` uploads the
job's output, the ``output`` term), predicted by the chosen row's ``combine``
term.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from meshforge.controller.planner.calibration import TERM_PHASE

__all__ = [
    "DATASET_VERSION",
    "FEATURES",
    "FEATURE_NAMES",
    "FEATURE_VERSION",
    "NUMERIC_FEATURES",
    "SCHEMA_FINGERPRINTS",
    "TERMS",
    "TERM_NAMES",
    "Feature",
    "FeatureGroup",
    "Scope",
    "Term",
    "schema_fingerprint",
    "term",
]

FEATURE_VERSION: Final = 1
DATASET_VERSION: Final = 1


class FeatureGroup(StrEnum):
    HARDWARE = "hardware"
    LOAD = "load"
    CACHE = "cache"
    NETWORK = "network"
    JOB_SIZE = "job_size"
    ANALYTICAL = "analytical"
    FAMILY = "family"


class Scope(StrEnum):
    """What one row of a term measures."""

    JOB = "job"
    """A whole job on one node (attempt role ``run``)."""
    LANE = "lane"
    """One lane of a split job (attempt role ``lane``)."""
    SET = "set"
    """A split job's set as a whole (its combine)."""


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    kind: str
    """``float`` (a number, NaN = missing) | ``category`` (a string, null =
    missing)."""
    unit: str
    group: FeatureGroup
    doc: str


def _f(name: str, unit: str, group: FeatureGroup, doc: str) -> Feature:
    return Feature(name, "float", unit, group, doc)


_H, _L, _C, _N, _J, _A = (
    FeatureGroup.HARDWARE,
    FeatureGroup.LOAD,
    FeatureGroup.CACHE,
    FeatureGroup.NETWORK,
    FeatureGroup.JOB_SIZE,
    FeatureGroup.ANALYTICAL,
)

FEATURES: Final[tuple[Feature, ...]] = (
    # --- hardware (the candidate node) ------------------------------------
    _f("hw_cpu_gflops", "GFLOP/s", _H, "Measured CPU score (all cores)."),
    _f("hw_gpu_tflops", "TFLOP/s", _H, "Best measured GPU fp32 score."),
    _f("hw_cpu_threads", "threads", _H, "Logical CPU threads."),
    _f("hw_ram_bytes", "bytes", _H, "Total RAM."),
    _f("hw_gpu_count", "GPUs", _H, "GPUs the node reported (0 = none)."),
    _f("hw_gpu_vram_max_bytes", "bytes", _H, "Largest GPU VRAM (NaN = no GPU)."),
    # --- load (at decision time) ------------------------------------------
    _f("load_cpu_percent", "%", _L, "CPU utilisation."),
    _f("load_ram_free_bytes", "bytes", _L, "Free RAM."),
    _f("load_live_attempts", "attempts", _L, "Live attempts on the node."),
    _f("load_used_cpu_slots", "slots", _L, "CPU slots in use."),
    _f("load_cpu_slots", "slots", _L, "CPU slots in total."),
    _f("load_owner_active", "flag", _L, "The owner is using the laptop."),
    # --- cache ----------------------------------------------------------------
    _f("cache_missing_bytes", "bytes", _C, "Job tree bytes the node lacks."),
    _f("cache_missing_frac", "ratio", _C, "Missing / input bytes (NaN: 0 input)."),
    _f("cache_env_cached", "flag", _C, "The job's last env is built there."),
    _f(
        "cache_items_tree_held",
        "flag",
        _C,
        "Split job: the node holds the items tree (NaN: no items tree).",
    ),
    # --- network --------------------------------------------------------------
    _f("net_local", "flag", _N, "The node is the submitter (Local)."),
    _f("net_ctl_bw_bytes_s", "bytes/s", _N, "Controller -> node bandwidth."),
    _f("net_peer_bw_bytes_s", "bytes/s", _N, "Best link from a tree holder."),
    _f("net_peer_rtt_ms", "ms", _N, "RTT of that link."),
    _f("net_peer_link_age_s", "s", _N, "Age of that link at decision time."),
    _f("net_term_bw_bytes_s", "bytes/s", _N, "Bandwidth the term itself used."),
    # --- job size -------------------------------------------------------------
    _f("job_input_bytes", "bytes", _J, "Distinct job tree bytes."),
    _f("job_items", "items", _J, "Split job: items (NaN: whole job)."),
    _f("job_item_bytes", "bytes", _J, "Split job: bytes of the items."),
    _f("job_chunk_items", "items", _J, "Split job: items per chunk planned."),
    _f("job_lanes", "lanes", _J, "Lanes of the chosen plan (1 = whole job)."),
    _f("job_cpus", "CPUs", _J, "Declared CPUs."),
    _f("job_ram_bytes", "bytes", _J, "Declared RAM."),
    _f("job_vram_bytes", "bytes", _J, "Declared VRAM."),
    _f("job_gpu", "flag", _J, "The job asks for a GPU."),
    _f("job_hist_output_bytes", "bytes", _J, "Median output bytes (history)."),
    # --- analytical -----------------------------------------------------------
    _f("an_raw_s", "s", _A, "Uncalibrated analytical prediction (s/item: rate)."),
    _f("an_src_measured", "flag", _A, "Term source = measured."),
    _f("an_src_history", "flag", _A, "Term source = history."),
    _f("an_src_scaled", "flag", _A, "Term source = scaled."),
    _f("an_src_assumed", "flag", _A, "Term source = assumed."),
    _f("an_term_samples", "runs", _A, "Samples behind the term (its detail)."),
    _f("an_hist_run_s", "s", _A, "Median run seconds on this node (history)."),
    _f("an_hist_samples", "runs", _A, "Runs behind that median (0 = none)."),
    # --- family (category) ----------------------------------------------------
    Feature(
        "family",
        "category",
        "-",
        FeatureGroup.FAMILY,
        "Workload family of the run record (null = unknown).",
    ),
)
FEATURE_NAMES: Final = tuple(f.name for f in FEATURES)
NUMERIC_FEATURES: Final = tuple(f.name for f in FEATURES if f.kind == "float")


@dataclass(frozen=True, slots=True)
class Term:
    name: str
    scope: Scope
    planner_term: str
    """The term's name in the stored candidate row's ``estimate.terms``
    (``chunk_rate``: the row's ``rate_items_s``)."""
    phase: str | None
    """The attempt phase that measures it (from the calibration's map);
    None = measured another way (:attr:`actual`)."""
    actual: str
    """What the actual value is, in words (the sidecar shows it)."""


def _phase_term(name: str, planner_term: str, scope: Scope) -> Term:
    phase = TERM_PHASE[planner_term]
    who = "the attempt's" if scope is Scope.JOB else "the lane attempt's"
    return Term(name, scope, planner_term, phase, f"{who} {phase} phase")


TERMS: Final[tuple[Term, ...]] = (
    _phase_term("transfer", "transfer", Scope.JOB),
    _phase_term("env", "env", Scope.JOB),
    _phase_term("compute", "compute", Scope.JOB),
    _phase_term("output", "output", Scope.JOB),
    _phase_term("lane_stage", "transfer", Scope.LANE),
    _phase_term("lane_env", "env", Scope.LANE),
    Term(
        "chunk_rate",
        Scope.LANE,
        "rate_items_s",
        None,
        "seconds per item: summed run_ms of the lane's committed chunks / their items",
    ),
    Term(
        "combine",
        Scope.SET,
        "combine",
        None,
        "the combine attempt's stage + env + run phases",
    ),
)
TERM_NAMES: Final = tuple(t.name for t in TERMS)
_BY_NAME: Final = {t.name: t for t in TERMS}


def term(name: str) -> Term:
    """The term called ``name`` (KeyError for an unknown one)."""
    return _BY_NAME[name]


def schema_fingerprint() -> str:
    """SHA-256 of the versions, features (name, kind, unit, group) and terms,
    in order: a unit test pins it per (dataset, feature) version."""
    payload = json.dumps(
        {
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "features": [[f.name, f.kind, f.unit, f.group.value] for f in FEATURES],
            "terms": [[t.name, t.scope.value, t.planner_term, t.phase] for t in TERMS],
        },
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


SCHEMA_FINGERPRINTS: Final[dict[tuple[int, int], str]] = {}
"""(dataset_version, feature_version) -> :func:`schema_fingerprint`; filled
in once the schema is final (a test checks the current one is listed)."""
