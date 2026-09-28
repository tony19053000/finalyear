"""Request and response models of ``/v1/jobs`` (brief D50).

The request is validated here for *shape* (types, lengths, no unknown keys); the
manifest inside it is validated by :mod:`meshforge.common.manifest` (the same model
the CLI and the agent use) and the placement by
:mod:`meshforge.controller.jobs.placement`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from meshforge.common.errors import FailureType
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.api.models import ApiModel, Utc

__all__ = [
    "AttemptSummary",
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
    """``{"node_id": "node_..."}``; ``local: true`` records that the submitter
    placed the job on its own laptop's agent (the CLI resolves ``node_id`` from the
    local ``node.json``; the controller cannot know which node "local" is)."""

    node_id: str = Field(min_length=1, max_length=40)
    local: bool = False


class JobSubmission(_Request):
    """Body of ``POST /v1/jobs``."""

    manifest: dict[str, Any]
    bundle_digest: str = Field(pattern=_DIGEST_PATTERN, max_length=71)
    placement: PlacementRequest


class Placement(ApiModel):
    mode: Literal["local", "offload"]
    """``local`` (Local baseline) or ``offload`` (Mode 1): the same path, recorded
    apart for telemetry."""
    node_id: str


class JobCreated(ApiModel):
    job_id: str
    state: JobState
    created: bool = Field(
        description="False when an Idempotency-Key replay returned an existing job."
    )


class TransitionOut(ApiModel):
    seq: int
    from_state: JobState | None
    to_state: JobState
    at: Utc


class AttemptSummary(ApiModel):
    attempt_id: str
    node_id: str
    fencing_token: int
    state: AttemptState
    failure_type: FailureType | None = None
    detail: str | None = None
    exit_code: int | None = None
    output_digest: str | None = None
    started_at: Utc | None = None
    ended_at: Utc | None = None


class JobSummary(ApiModel):
    job_id: str
    name: str
    state: JobState
    bundle_digest: str
    placement: Placement
    failure_type: FailureType | None = None
    failure_detail: str | None = None
    created_at: Utc
    updated_at: Utc


class JobDetail(JobSummary):
    manifest: dict[str, Any]
    transitions: tuple[TransitionOut, ...]
    attempts: tuple[AttemptSummary, ...]


class JobList(ApiModel):
    jobs: tuple[JobSummary, ...]
    count: int
