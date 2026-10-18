"""Request and response models of ``/v1/jobs`` (brief D50).

The request is validated here for *shape* (types, lengths, no unknown keys); the
manifest inside it is validated by :mod:`meshforge.common.manifest` (the same model
the CLI and the agent use) and the placement by
:mod:`meshforge.controller.jobs.placement`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meshforge.common.errors import FailureType
from meshforge.common.manifest import PolicyName
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.api.models import ApiModel, Utc

__all__ = [
    "AttemptSummary",
    "BenchmarkRef",
    "JobCancelled",
    "JobCreated",
    "JobDetail",
    "JobList",
    "JobSubmission",
    "JobSummary",
    "Placement",
    "PlacementRequest",
    "TransitionOut",
]

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PlacementRequest(_Request):
    """Where the job may run (V0.6 D103).

    * ``node_id`` given = **pinned**: the job runs on that node or nowhere (the
      V0.4 manual path, unchanged). ``local: true`` records that the submitter
      pinned it to its own laptop's agent (the CLI resolves ``node_id`` from
      the local ``node.json``; the controller cannot know which node "local"
      is).
    * ``node_id`` omitted = **auto**: the planner chooses among every node
      (the submitter's own node, when named in ``submitter_node_id``, is the
      Local candidate).
    * ``policy`` (D107) overrides the manifest's ``policy`` and the
      controller's ``[controller.planner] default_policy``.
    """

    node_id: str | None = Field(default=None, min_length=1, max_length=40)
    local: bool = False
    policy: PolicyName | None = None

    @model_validator(mode="after")
    def _local_needs_node(self) -> PlacementRequest:
        if self.local and self.node_id is None:
            raise ValueError("local: true needs the local node's node_id")
        return self


class JobSubmission(_Request):
    """Body of ``POST /v1/jobs``: exactly one of ``tree_id`` (V0.5 D83, what
    ``compute run`` sends) and ``bundle_digest`` (the V0.4 bundle path, kept for
    existing callers). ``submitter_node_id`` (V0.6 D103) names the submitting
    laptop's own enrolled node - its Local candidate; ``seeded`` (D109) says the
    CLI put the project's blobs into that node's cache."""

    manifest: dict[str, Any]
    bundle_digest: str | None = Field(
        default=None, pattern=_DIGEST_PATTERN, max_length=71
    )
    tree_id: str | None = Field(default=None, pattern=_DIGEST_PATTERN, max_length=71)
    placement: PlacementRequest = Field(default_factory=PlacementRequest)
    submitter_node_id: str | None = Field(default=None, min_length=1, max_length=40)
    seeded: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> JobSubmission:
        if (self.bundle_digest is None) == (self.tree_id is None):
            raise ValueError("give exactly one of tree_id and bundle_digest")
        if self.seeded and self.submitter_node_id is None:
            raise ValueError("seeded: true needs submitter_node_id")
        return self


class Placement(ApiModel):
    mode: Literal["local", "offload", "auto"]
    """``local`` (pinned to the submitter's own node), ``offload`` (pinned to
    another node, Mode 1) or ``auto`` (the planner places it, V0.6). An auto
    job's actual node and mode are in its attempts and ``/decisions``."""
    node_id: str | None = None
    """The pinned node; None for an auto job."""
    policy: PolicyName | None = Field(default=None, exclude_if=lambda v: v is None)
    """The policy asked for at submit; left out when none was (the manifest's
    or the controller's default applies) - a pinned V0.4 job's placement reads
    exactly as before."""


class JobCreated(ApiModel):
    job_id: str
    state: JobState
    created: bool = Field(
        description="False when an Idempotency-Key replay returned an existing job."
    )


class JobCancelled(ApiModel):
    job_id: str
    state: JobState
    cancel_requested: bool = Field(
        description=(
            "True while the job waits for its agent to stop the running attempt; "
            "False when the job is already in its final state."
        )
    )


class TransitionOut(ApiModel):
    seq: int
    from_state: JobState | None
    to_state: JobState
    at: Utc


class FinalLogSummary(ApiModel):
    """One stream's final log (D71): uploaded and verified, or only live."""

    digest: str | None = None
    bytes: int = 0
    truncated: bool = False


class TransferOut(ApiModel):
    """One transfer_event row (V0.5 G5, D93): ``source`` is ``cache`` (hits:
    ``units`` blobs / ``bytes`` already cached, ``duration_ms`` = the tree's
    whole staging), ``controller`` or a peer's node id (bytes received, request
    time, units won, ``dropped`` = why it was dropped)."""

    tree_id: str
    source: str
    bytes: int
    duration_ms: int
    units: int
    failures: int
    resumed_chunks: int = 0
    dropped: str | None = None


class InputOut(ApiModel):
    """A named input the job was submitted with (V0.5 G5, D92)."""

    ref: str
    name: str
    version: int
    tree_id: str
    mount: str


class AttemptSummary(ApiModel):
    attempt_id: str
    node_id: str
    fencing_token: int
    state: AttemptState
    failure_type: FailureType | None = None
    detail: str | None = None
    exit_code: int | None = None
    output_digest: str | None = None
    """The packed outputs (``compute get``); verified under the attempt's grant."""
    missing_outputs: list[str] = Field(default_factory=list)
    """Declared outputs that matched nothing (D74) - listed, not an error."""
    logs: dict[str, FinalLogSummary] = Field(default_factory=dict)
    """Final logs per stream (D71); empty until the attempt ended with them."""
    started_at: Utc | None = None
    ended_at: Utc | None = None
    transfers: list[TransferOut] = Field(default_factory=list)
    """What staging cost per tree and source (V0.5 G5); empty until it ended."""


class BenchmarkRef(ApiModel):
    """V0.9 G3 (D215/D232): the campaign run this job is (``bench_run.job_id``)."""

    campaign_id: str
    run_id: str


class JobSummary(ApiModel):
    job_id: str
    name: str
    state: JobState
    bundle_digest: str | None = None
    """The V0.4 bundle; None for a tree job."""
    tree_id: str | None = None
    """V0.5 D83: the project's tree manifest; None for a V0.4 bundle job."""
    placement: Placement
    failure_type: FailureType | None = None
    failure_detail: str | None = None
    created_at: Utc
    updated_at: Utc
    priority: int = 0
    """D106 (manifest ``priority``)."""
    submitter_node_id: str | None = None
    """D103: the submitting laptop's node (its Local candidate), if named."""
    benchmark: BenchmarkRef | None = None
    """V0.9 G3: set when the job is a benchmark campaign's forced run."""


class JobDetail(JobSummary):
    manifest: dict[str, Any]
    transitions: tuple[TransitionOut, ...]
    attempts: tuple[AttemptSummary, ...]
    inputs: tuple[InputOut, ...] = ()


class JobList(ApiModel):
    jobs: tuple[JobSummary, ...]
    count: int
    """Jobs on this page."""
    next_cursor: str | None = None
    """V0.8 G1 (D174): pass as ``?cursor=`` for the next page; None = last page."""
