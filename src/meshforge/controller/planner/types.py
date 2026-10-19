"""The planner's inputs and outputs as frozen values (V0.6 G2, D102).

Everything here is plain data: no database rows, no sessions, no clocks. The
dispatcher builds one :class:`ClusterSnapshot` per dispatch tick and one
:class:`PlanRequest` per job; :func:`~.plan.plan` turns them into a
:class:`Decision`. Same inputs -> same decision (checklist 16).

Units: bytes, seconds, bytes/s. Nothing in the planner reads a clock: ``now``
is an argument of :func:`~.plan.plan` (D102).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from meshforge.common.owner_policy import OwnerPolicy

__all__ = [
    "STATIC_CODES",
    "CandidateEval",
    "ClusterSnapshot",
    "ControllerLinkStat",
    "Decision",
    "EnvBuildStat",
    "Estimate",
    "EstimateTerm",
    "GpuNeed",
    "History",
    "LanePlan",
    "LinkStat",
    "MarginTest",
    "Mode",
    "NodeSnapshot",
    "PlanRequest",
    "PlannerConfig",
    "RejectCode",
    "Rejection",
    "Slot",
    "SplitConfig",
    "SplitInfo",
    "SplitPlan",
    "TermSource",
    "TreeNeed",
]

type Slot = Literal["cpu", "gpu"]
type GpuNeed = Literal["required", "preferred", "none"]
type Mode = Literal["local", "offload", "split"]
"""``local`` / ``offload`` = one node runs the job (for a split job: one lane,
"don't split"); ``split`` = Mode 3 over a node set (V0.10 G3, D265)."""


class RejectCode(StrEnum):
    """Why a node cannot take a job. Values are stored in ``plan_decision`` and
    shown by ``compute explain``: never rename, only add.

    *Static* codes depend only on what the node is (hardware, capabilities,
    owner settings) - a job no node passes statically can never run and is
    refused at submit (D104). *Live* codes depend on the moment (liveness,
    load, slots, recent failures) - the job waits.
    """

    # --- static: the node can never run this job --------------------------------
    NOT_EXEC = "not_exec"
    """The owner has not enabled execution (no ``exec`` capability)."""
    NO_GPU = "no_gpu"
    """The job requires a GPU; the node has none."""
    NO_GPU_RUNTIME = "no_gpu_runtime"
    """The node has a GPU but no runtime that can pass it to a job."""
    NO_GPU_SLOTS = "no_gpu_slots"
    """The owner allows no GPU jobs on this node (``max_gpu_jobs = 0``)."""
    NO_CPU_SLOTS = "no_cpu_slots"
    """The owner allows no CPU jobs on this node (``max_cpu_jobs = 0``)."""
    VRAM_SHORT = "vram_short"
    RAM_SHORT = "ram_short"
    CPUS_SHORT = "cpus_short"
    NO_RUNTIME_FOR_BASE = "no_runtime_for_base"
    """``cuda-pytorch`` needs Docker or the subprocess runtime; this node only
    offers the venv runtime (which provides ``python-cpu`` only)."""
    REVOKED = "revoked"
    """The node was revoked from the fabric."""
    NO_SPLIT_SUPPORT = "no_split_support"
    """V0.10 G4 (D263): a split job, and the node's agent cannot run a lane
    (no ``exec-split`` capability: older than V0.10) - it would run the lane
    as one whole attempt. Split jobs only."""
    # V0.7 G5 (D142): the node owner's policy refuses the job. Static: they
    # depend on what the owner allows, not on the moment - a job every node's
    # policy refuses fails (no_feasible_plan) instead of waiting. The values
    # are :class:`~meshforge.common.owner_policy.PolicyCode`'s.
    POLICY_NOT_ACCEPTING = "policy_not_accepting"
    POLICY_USER_NOT_ALLOWED = "policy_user_not_allowed"
    POLICY_RAM = "policy_ram"
    POLICY_CPU = "policy_cpu"
    POLICY_VRAM = "policy_vram"
    POLICY_UNSANDBOXED = "policy_unsandboxed"
    POLICY_NETWORK = "policy_network"
    # --- live: not now -----------------------------------------------------------
    NOT_ONLINE = "not_online"
    """The node is SUSPECT / OFFLINE / DRAINING / ENROLLING."""
    NO_SESSION = "no_session"
    """ONLINE in the database but no live stream with this controller."""
    SLOTS_FULL = "slots_full"
    """Every slot the job could use is taken."""
    NODE_CAP = "node_cap"
    """``[controller.jobs] max_attempts_per_node`` live attempts already."""
    ENV_KNOWN_BAD = "env_known_bad"
    """This job's environment failed to build on this node recently (D116)."""
    DISK_SHORT = "disk_short"
    """The node's latest sample shows less free disk than the job's inputs."""
    RETRY_EXCLUDED = "retry_excluded"
    """V0.6 G3 (D110): an earlier attempt of this job failed here in a way
    another node may avoid (lost, transfer/integrity error, timeout, or a
    GPU no larger than the one that ran out of VRAM). Auto jobs only; lifted
    by the D129 rule (:mod:`.retry_exclusion`) when no other node could ever
    run the job."""
    # --- V0.10 G3 (D265): a Mode 3 *set* candidate, not a node ------------------
    SPLIT_TOO_SMALL = "split_too_small"
    """Fewer than ``2 x min_nodes`` items: the job runs unsplit (forced "don't
    split"). Not in :data:`STATIC_CODES`: set rows never decide whether the
    job can run at all (:attr:`Decision.all_static_rejections` reads node
    rows only)."""
    SPLIT_TOO_FEW_NODES = "split_too_few_nodes"
    """Fewer than ``min_nodes`` nodes can take a lane right now."""
    BENCH_PAUSED = "policy_bench_paused"
    """V0.9 G5 (D219/D246): a **benchmark** job, and the node's owner paused
    benchmarks or is using the laptop (the heartbeat's ``owner_active``). Live
    on purpose - it is a moment, not a limit: the bench job waits, never fails
    ``no_feasible_plan``. A user job is never refused for it. The value is
    :attr:`~meshforge.common.owner_policy.PolicyCode.BENCH_PAUSED`'s."""


STATIC_CODES: frozenset[RejectCode] = frozenset(
    {
        RejectCode.NOT_EXEC,
        RejectCode.NO_SPLIT_SUPPORT,
        RejectCode.NO_GPU,
        RejectCode.NO_GPU_RUNTIME,
        RejectCode.NO_GPU_SLOTS,
        RejectCode.NO_CPU_SLOTS,
        RejectCode.VRAM_SHORT,
        RejectCode.RAM_SHORT,
        RejectCode.CPUS_SHORT,
        RejectCode.NO_RUNTIME_FOR_BASE,
        RejectCode.REVOKED,
        RejectCode.POLICY_NOT_ACCEPTING,
        RejectCode.POLICY_USER_NOT_ALLOWED,
        RejectCode.POLICY_RAM,
        RejectCode.POLICY_CPU,
        RejectCode.POLICY_VRAM,
        RejectCode.POLICY_UNSANDBOXED,
        RejectCode.POLICY_NETWORK,
    }
)


class TermSource(StrEnum):
    """Where an estimate's number came from (D108, checklist 18)."""

    MEASURED = "measured"
    """Observed for this node right now (cache index, slots)."""
    HISTORY = "history"
    """Median of past runs of this workload on this node."""
    SCALED = "scaled"
    """Past runs on another node, scaled by the hardware-score ratio."""
    ASSUMED = "assumed"
    """A configured default: nothing measured supports it."""


@dataclass(frozen=True, slots=True)
class Rejection:
    code: RejectCode
    text: str
    """Short, ours, safe to print (never a value from the network)."""

    @property
    def static(self) -> bool:
        return self.code in STATIC_CODES


@dataclass(frozen=True, slots=True)
class TreeNeed:
    """One tree (or bundle) the job reads, and how big it is."""

    key: str
    """``sha256:<hex>`` tree id (or bundle digest)."""
    size: int


@dataclass(frozen=True, slots=True)
class History:
    """Past runs of one workload fingerprint (D108): medians of the ``run``
    phase of successful attempts."""

    by_node_s: Mapping[str, float] = field(default_factory=dict)
    """node_id -> median run seconds on that node."""
    samples_by_node: Mapping[str, int] = field(default_factory=dict)
    output_bytes: float | None = None
    """V0.6 G4 (D108): median bytes the fingerprint's successful attempts
    uploaded (outputs + final logs); None = never recorded."""
    output_samples: int = 0
    env_hash_by_node: Mapping[str, str] = field(default_factory=dict)
    """node_id -> the env hash the fingerprint's latest attempt there reported
    (with :attr:`NodeSnapshot.cached_envs`: is the env still built there?)."""
    item_rate_by_node: Mapping[str, float] = field(default_factory=dict)
    """V0.10 G3 (D265): node_id -> steady items/s of this fingerprint's chunks
    on that node (chunk history: median of committed chunks' ``items / run
    seconds``); empty until chunk records exist (G8)."""
    item_rate_samples_by_node: Mapping[str, int] = field(default_factory=dict)
    chunk_overhead_s: float | None = None
    """V0.10 G3 (D265): median per-chunk overhead of this fingerprint (process
    start + chunk stage + result upload); None = never measured."""
    chunk_overhead_samples: int = 0


@dataclass(frozen=True, slots=True)
class SplitInfo:
    """A split job as the planner sees it (V0.10 G3, D265): the items resolved
    at submit (G1) and the manifest's chunk and node bounds."""

    items: int
    """Items to run (all of them, or the remainder on a re-plan, D267e)."""
    item_bytes: int | None = None
    """Bytes the items stand for (glob files / ``lines`` records); None for
    ``list`` / ``range`` items (no data of their own)."""
    items_tree: str | None = None
    """The tree (a key of :attr:`PlanRequest.trees`) the item bytes live in: a
    lane stages that tree **minus** the items once and fetches each chunk's
    items when it runs the chunk (D264). None = the items carry no bytes."""
    chunk_items: int | None = None
    """``split.chunk.items``; None = ``auto`` (the planner sizes chunks)."""
    min_nodes: int = 2
    max_nodes: int | None = None
    """None = every suitable node."""

    @property
    def bytes_per_item(self) -> float:
        if not self.item_bytes or self.item_bytes < 0 or self.items <= 0:
            return 0.0
        return self.item_bytes / self.items


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """One job as the planner sees it."""

    job_id: str
    attempt_no: int
    """The fencing token the next attempt will get (1 for the first)."""
    gpu: GpuNeed
    vram_bytes: int | None
    ram_bytes: int | None
    cpus: float | None
    base: str
    """Manifest ``env.base``."""
    env_key: str
    """:func:`~meshforge.controller.jobs.env_health.env_key` of the job."""
    trees: tuple[TreeNeed, ...] = ()
    """Project tree (or bundle) first, then named inputs."""
    pinned_node_id: str | None = None
    """D103: given = the candidate set is this one node."""
    submitter_node_id: str | None = None
    """The Local candidate (D103, ARCHITECTURE §5)."""
    seeded: bool = False
    """D109: the submitter's CAS already holds the project tree."""
    fingerprint: str | None = None
    history: History = field(default_factory=History)
    excluded: Mapping[str, str] = field(default_factory=dict)
    """V0.6 G3 (D110): node_id -> the failure type that excludes it from this
    retry (see :mod:`.retry_exclusion`); ignored for a pinned job."""
    owner_name: str | None = None
    """V0.7 G5 (D142): the job owner's user name (owner policies'
    ``allowed_users``); None = unknown (legacy fabric-token job)."""
    network: bool = False
    """V0.7 G5 (D143): the manifest asks ``limits.network: on``."""
    benchmark: bool = False
    """V0.9 G5 (D246): a benchmark job (``bench:`` identity, D237): refused
    live where the owner guard is on (:attr:`RejectCode.BENCH_PAUSED`)."""
    split: SplitInfo | None = None
    """V0.10 G3 (D265): a split job's items; None = the job runs whole."""

    @property
    def is_gpu_job(self) -> bool:
        return self.gpu != "none"


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """One node at the moment of the snapshot."""

    node_id: str
    state: str
    """Persisted liveness state (``online``, ``suspect``...)."""
    connected: bool
    """A live stream with this controller."""
    capabilities: frozenset[str]
    ram_bytes: int | None = None
    cpu_threads: int | None = None
    gpu_vram_bytes: tuple[int, ...] = ()
    """VRAM of each GPU (0 = the node did not say)."""
    gpu_indices: tuple[int, ...] = ()
    """NVML index of each GPU, parallel to ``gpu_vram_bytes`` (empty = the
    positions are the indices)."""
    busy_gpus: frozenset[int] = frozenset()
    """NVML indices held by live attempts on this node: a GPU serves one job
    at a time, so a GPU slot is free only while a GPU is (G2 fix D-2)."""
    cpu_slots: int = 1
    gpu_slots: int = 0
    used_cpu_slots: int = 0
    used_gpu_slots: int = 0
    live_attempts: int = 0
    """Every live attempt on the node (both slot kinds)."""
    cpu_percent: float | None = None
    ram_free_bytes: int | None = None
    disk_free_bytes: int | None = None
    cpu_score: float | None = None
    """``cpu_fp32_gflops_all`` mean, if measured ok."""
    gpu_score: float | None = None
    """Best ``gpu_fp32_tflops`` mean over the node's GPUs, if measured ok."""
    cached: frozenset[str] = frozenset()
    """Tree ids / digests (from the request's trees) this node holds whole."""
    known_bad_envs: frozenset[str] = frozenset()
    """Env keys that failed to build here recently (D116)."""
    cached_envs: Mapping[str, int] = field(default_factory=dict)
    """V0.6 G4: env hash -> bytes of the built environments this node holds
    (cache index, only the hashes the tick's jobs care about)."""
    policy: OwnerPolicy | None = None
    """V0.7 G5 (D142): the owner policy the node advertises on its live session;
    None = not connected or an agent from before G5 (no owner restriction is
    assumed here - that agent still enforces its own opt-ins)."""
    owner_active: bool = False
    """V0.9 G5 (D246): the node's last heartbeat says its owner is using it
    (benchmark jobs only wait for it)."""

    @property
    def free_cpu_slots(self) -> int:
        return max(0, self.cpu_slots - self.used_cpu_slots)

    @property
    def free_gpu_vram_bytes(self) -> tuple[int, ...]:
        """VRAM of each GPU no live attempt holds."""
        indices = self.gpu_indices or tuple(range(len(self.gpu_vram_bytes)))
        return tuple(
            vram
            for index, vram in zip(indices, self.gpu_vram_bytes, strict=False)
            if index not in self.busy_gpus
        )

    @property
    def free_gpu_slots(self) -> int:
        """GPU slots not in use, and never more than the GPUs not in use."""
        return max(
            0,
            min(
                self.gpu_slots - self.used_gpu_slots,
                len(self.free_gpu_vram_bytes),
            ),
        )


@dataclass(frozen=True, slots=True)
class LinkStat:
    """V0.6 G4 (D111): one measured link - ``src`` downloaded from ``dst``'s
    peer server (data flows dst -> src). EWMA values."""

    bw_bytes_s: float
    rtt_ms: float
    samples: int
    measured_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ControllerLinkStat:
    """V0.6 G4 (D111): controller -> node bandwidth from transfer history
    (median of recent controller transfers)."""

    bw_bytes_s: float
    samples: int
    last_at: dt.datetime | None = None
    """When the newest of those transfers was recorded (D131: a ``history``
    transfer term shows its age; there is no staleness rule for it)."""


@dataclass(frozen=True, slots=True)
class EnvBuildStat:
    """V0.6 G4 (D108): median environment build time for one ``env.base``
    (env phases long enough to be a build, not a cache hit)."""

    median_s: float
    samples: int


@dataclass(frozen=True, slots=True)
class SplitConfig:
    """``[controller.split]`` values the planner needs (V0.10 G3, D275)."""

    split_margin: float = 0.10
    """Split only when the best set is predicted at least this fraction faster
    than the best single node (noise must not flip the decision)."""
    target_chunk_s: float = 20.0
    """Auto chunk size: about this many seconds on the fastest node of the set."""
    max_overhead_frac: float = 0.05
    """Auto chunk size is raised until the per-chunk overhead is at most this
    fraction of a chunk's time on the fastest node ..."""
    min_chunks_per_node: int = 4
    """... and lowered until there are at least this many chunks per node
    (tail balance; wins over the overhead rule)."""
    max_chunks: int = 10_000
    """``[controller.split] max_chunks``: auto chunking never makes more."""
    chunk_overhead_s: float = 1.5
    """Assumed per-chunk overhead (process start + chunk stage + upload) until
    chunk history measures it."""
    tail_frac: float = 0.5
    """Expected tail of a set: this fraction of its longest chunk time (the
    lanes do not finish together; pulling keeps it under one chunk)."""


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """``[controller.planner]`` values the pure planner needs."""

    default_bw_bytes_s: float = 50e6
    default_compute_s: float = 60.0
    default_env_s: float = 0.0
    max_attempts_per_node: int = 8
    resource_weights: tuple[float, float, float] = (1.0, 1.0, 1.0)
    """``resource_score``: (free RAM GiB, free slots, normalised score)."""
    link_stale_after_s: float = 3600.0
    """V0.6 G4 (D111): a link measured longer ago than this is not used."""
    min_bw_bytes_s: float = 1024.0
    """D131 (``[controller.network] min_bw_bytes_s``): no estimate divides by
    a bandwidth below this, so every estimate is finite."""
    split: SplitConfig = field(default_factory=SplitConfig)
    """V0.10 G3 (D275)."""


@dataclass(frozen=True, slots=True)
class ClusterSnapshot:
    nodes: tuple[NodeSnapshot, ...]
    """Sorted by node_id (the builder sorts; ties break on it everywhere)."""
    round_robin_after: str | None = None
    """``round_robin``'s cursor: the node chosen last (D107; in memory)."""
    config: PlannerConfig = field(default_factory=PlannerConfig)
    links: Mapping[tuple[str, str], LinkStat] = field(default_factory=dict)
    """V0.6 G4: ``(src, dst) -> LinkStat`` - the network matrix (stale rows
    included; the estimator judges age against ``now``)."""
    controller_links: Mapping[str, ControllerLinkStat] = field(default_factory=dict)
    """V0.6 G4: node_id -> controller -> node bandwidth (transfer history)."""
    env_builds: Mapping[str, EnvBuildStat] = field(default_factory=dict)
    """V0.6 G4: ``env.base`` -> environment build time history."""

    def node(self, node_id: str) -> NodeSnapshot | None:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None


@dataclass(frozen=True, slots=True)
class EstimateTerm:
    seconds: float
    source: TermSource
    detail: Mapping[str, Any] = field(default_factory=dict)
    """The numbers behind it (bytes, bandwidth...) for ``compute explain``."""


@dataclass(frozen=True, slots=True)
class Estimate:
    """Predicted completion time of one candidate (D108 terms)."""

    queue: EstimateTerm
    transfer: EstimateTerm
    env: EstimateTerm
    compute: EstimateTerm
    output: EstimateTerm
    extra: Mapping[str, EstimateTerm] = field(default_factory=dict)
    """V0.10 G3: the terms only a split job has (``items_fetch``,
    ``chunk_overhead``, ``tail``, ``combine``), in that order; empty for a job
    that runs whole."""

    @property
    def total_s(self) -> float:
        return (
            self.queue.seconds
            + self.transfer.seconds
            + self.env.seconds
            + self.compute.seconds
            + self.output.seconds
            + sum(term.seconds for term in self.extra.values())
        )

    def terms(self) -> dict[str, EstimateTerm]:
        return {
            "queue": self.queue,
            "transfer": self.transfer,
            "env": self.env,
            "compute": self.compute,
            "output": self.output,
            **self.extra,
        }


@dataclass(frozen=True, slots=True)
class LanePlan:
    """One node of a split job's plan (V0.10 G3): a lane the dispatcher
    creates (D259)."""

    node_id: str
    slot: Slot
    local: bool
    """The submitter's own node."""
    rate_items_s: float
    """Predicted items/s of compute alone (D265 ``r_n``)."""
    rate_source: TermSource
    start_s: float
    """Predicted stage + env seconds before its first chunk runs."""
    active: bool = True
    """False = predicted to be ready only after the other lanes finish (it
    would get no chunk); kept so the plan shows it."""


@dataclass(frozen=True, slots=True)
class MarginTest:
    """D265: split only when ``set_s <= (1 - margin) x best_single_s``."""

    set_s: float | None
    """The set's prediction; None = no set was compared (``forced``)."""
    best_single_s: float | None
    """The best single node's prediction (Local or Mode 1, one lane)."""
    best_single_node_id: str | None
    margin: float
    threshold_s: float | None
    """``(1 - margin) x best_single_s``."""
    passed: bool
    """True = splitting is worth it."""
    forced: str | None = None
    """Why no set was compared: ``pinned``, ``split_too_small`` or
    ``split_too_few_nodes``; None = the test ran."""


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """**What the dispatcher runs for a split job (the G3 -> G4 interface).**

    Present on :attr:`Decision.split` whenever a split job's decision chose
    something. ``split`` False = "don't split": one lane on
    :attr:`Decision.chosen` (Local or Mode 1) - the same lane path (D269).
    ``lanes`` are in rate order, fastest first, one per node (D267f); the
    inactive ones (predicted to get no chunk) are not in it."""

    split: bool
    lanes: tuple[LanePlan, ...]
    chunk_items: int
    """Items per chunk (the last chunk may hold fewer)."""
    chunks: int
    predicted_makespan_s: float
    """The chosen candidate's ``estimate.total_s``."""
    margin: MarginTest

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(lane.node_id for lane in self.lanes)


@dataclass(frozen=True, slots=True)
class CandidateEval:
    node_id: str
    mode: Mode
    feasible: bool
    rejection: Rejection | None = None
    slot: Slot | None = None
    """The slot it would take (feasible candidates only)."""
    estimate: Estimate | None = None
    score: float | None = None
    """The policy's sort value (what ``rank`` was ordered by)."""
    rank: int | None = None
    """1 = chosen; None = not feasible."""
    node_set: tuple[str, ...] = ()
    """V0.10 G3, split jobs only: the lanes' nodes, fastest first (one node for
    Local / Mode 1, the set for ``split``); ``node_id`` is the first."""
    lanes: tuple[LanePlan, ...] = ()
    chunk_items: int | None = None
    chunks: int | None = None
    margin: MarginTest | None = None
    """``split`` candidates: this set against the best single node."""


@dataclass(frozen=True, slots=True)
class Decision:
    policy: str
    candidates: tuple[CandidateEval, ...]
    chosen: CandidateEval | None
    snapshot_digest: str
    reason_key: str
    split: SplitPlan | None = None
    """V0.10 G3: what to run for a split job (None = not a split job, or
    nothing chosen)."""

    @property
    def all_static_rejections(self) -> bool:
        """No node candidate at all, or every one rejected for a static reason:
        the job can never run on the nodes this snapshot knows (D104). Mode 3
        set rows derive from the node rows and never decide it."""
        return all(
            c.rejection is not None and c.rejection.static
            for c in self.candidates
            if c.mode != "split"
        )
