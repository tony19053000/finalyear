"""The planner's inputs and outputs as frozen values (V0.6 G2, D102).

Everything here is plain data: no database rows, no sessions, no clocks. The
dispatcher builds one :class:`ClusterSnapshot` per dispatch tick and one
:class:`PlanRequest` per job; :func:`~.plan.plan` turns them into a
:class:`Decision`. Same inputs -> same decision (checklist 16).

Units: bytes, seconds, bytes/s. Nothing in the planner reads a clock: ``now``
is an argument of :func:`~.plan.plan` (D102).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "STATIC_CODES",
    "CandidateEval",
    "ClusterSnapshot",
    "Decision",
    "Estimate",
    "EstimateTerm",
    "GpuNeed",
    "History",
    "Mode",
    "NodeSnapshot",
    "PlanRequest",
    "PlannerConfig",
    "RejectCode",
    "Rejection",
    "Slot",
    "TermSource",
    "TreeNeed",
]

type Slot = Literal["cpu", "gpu"]
type GpuNeed = Literal["required", "preferred", "none"]
type Mode = Literal["local", "offload"]


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


STATIC_CODES: frozenset[RejectCode] = frozenset(
    {
        RejectCode.NOT_EXEC,
        RejectCode.NO_GPU,
        RejectCode.NO_GPU_RUNTIME,
        RejectCode.NO_GPU_SLOTS,
        RejectCode.NO_CPU_SLOTS,
        RejectCode.VRAM_SHORT,
        RejectCode.RAM_SHORT,
        RejectCode.CPUS_SHORT,
        RejectCode.NO_RUNTIME_FOR_BASE,
        RejectCode.REVOKED,
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

    @property
    def free_cpu_slots(self) -> int:
        return max(0, self.cpu_slots - self.used_cpu_slots)

    @property
    def free_gpu_slots(self) -> int:
        return max(0, self.gpu_slots - self.used_gpu_slots)


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """``[controller.planner]`` values the pure planner needs."""

    default_bw_bytes_s: float = 50e6
    default_compute_s: float = 60.0
    default_env_s: float = 0.0
    max_attempts_per_node: int = 8
    resource_weights: tuple[float, float, float] = (1.0, 1.0, 1.0)
    """``resource_score``: (free RAM GiB, free slots, normalised score)."""


@dataclass(frozen=True, slots=True)
class ClusterSnapshot:
    nodes: tuple[NodeSnapshot, ...]
    """Sorted by node_id (the builder sorts; ties break on it everywhere)."""
    round_robin_after: str | None = None
    """``round_robin``'s cursor: the node chosen last (D107; in memory)."""
    config: PlannerConfig = field(default_factory=PlannerConfig)

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

    @property
    def total_s(self) -> float:
        return (
            self.queue.seconds
            + self.transfer.seconds
            + self.env.seconds
            + self.compute.seconds
            + self.output.seconds
        )

    def terms(self) -> dict[str, EstimateTerm]:
        return {
            "queue": self.queue,
            "transfer": self.transfer,
            "env": self.env,
            "compute": self.compute,
            "output": self.output,
        }


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


@dataclass(frozen=True, slots=True)
class Decision:
    policy: str
    candidates: tuple[CandidateEval, ...]
    chosen: CandidateEval | None
    snapshot_digest: str
    reason_key: str

    @property
    def all_static_rejections(self) -> bool:
        """No candidate at all, or every one rejected for a static reason: the
        job can never run on the nodes this snapshot knows (D104)."""
        return all(
            c.rejection is not None and c.rejection.static for c in self.candidates
        )
