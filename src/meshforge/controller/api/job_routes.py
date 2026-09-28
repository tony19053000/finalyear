"""``POST /v1/jobs``, ``GET /v1/jobs``, ``GET /v1/jobs/{job_id}`` (brief D50).

Every route needs the fabric token (D44: job reads expose user output).

**Submit** ``{manifest, bundle_digest, placement: {node_id, local?}}`` with an
optional ``Idempotency-Key`` header (1-128 printable ASCII characters):

1. the body is read with a hard size cap and validated (shape, then the manifest
   with the same model the CLI and agent use - errors carry YAML paths);
2. a key already used with the **same** canonical request returns that job
   (``200``, ``created: false``: no second job, no second execution); with a
   **different** request it is ``409 conflict``;
3. the bundle must already be in the blob store (``409 conflict`` naming the
   digest - upload it with ``PUT /v1/blobs/{sha256}`` first);
4. placement is checked (``404`` unknown node; ``409 no_feasible_plan`` for a node
   that is not online, has no live session, does not accept jobs, or cannot give
   the job the GPU/VRAM/RAM/CPUs it asks for);
5. only then is the job written: ``SUBMITTED -> PLANNING -> QUEUED``, each arrow
   recorded with its time (``PLANNING`` is a pass-through while placement is
   manual). Answer ``201 {job_id, state: "queued", created: true}``.

**A rejected request never creates a row.** Concurrent duplicate POSTs with one
key produce one row (the unique index), and every loser gets the winner's job.

**Nothing dispatches yet (V0.4 G4):** a ``queued`` job stays ``queued``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import ValidationError

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_job_id, validate_job_id, validate_node_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import ManifestError, parse_manifest_data
from meshforge.common.states import JobState
from meshforge.controller.api.auth import FabricTokenRequired
from meshforge.controller.api.context import FabricView, get_fabric
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_models import (
    AttemptSummary,
    JobCreated,
    JobDetail,
    JobList,
    JobSubmission,
    JobSummary,
    Placement,
    TransitionOut,
)
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.placement import PlacementError, check_placement
from meshforge.controller.jobs.store import (
    AttemptRecord,
    IdempotencyConflictError,
    JobRecord,
    JobStore,
)
from meshforge.controller.jobs.transitions import JobEvent

__all__ = [
    "DEFAULT_JOB_LIMIT",
    "IDEMPOTENCY_KEY_MAX_CHARS",
    "JOBS_STATE_KEY",
    "MAX_JOB_LIMIT",
    "JobApi",
    "job_router",
    "request_hash",
]

log = get_logger(__name__)

JOBS_STATE_KEY: Final = "meshforge_jobs"
DEFAULT_JOB_LIMIT: Final = 50
MAX_JOB_LIMIT: Final = 500
IDEMPOTENCY_KEY_MAX_CHARS: Final = 128
_KEY_RE: Final = re.compile(r"[\x21-\x7e]{1,128}")
_JOB_ID_MAX: Final = 40


class JobApi:
    """What the job routes need; put on ``app.state`` by ``create_http_app``."""

    def __init__(
        self, *, store: JobStore, blobs: BlobStore, max_request_bytes: int
    ) -> None:
        self.store = store
        self.blobs = blobs
        self.max_request_bytes = max_request_bytes


def get_job_api(request: Request) -> JobApi:
    api = getattr(request.app.state, JOBS_STATE_KEY, None)
    if not isinstance(api, JobApi):
        raise ApiError(FailureType.CONFIG_ERROR, "the job API is not configured")
    return api


job_router = APIRouter(prefix="/v1/jobs", tags=["jobs"])
Jobs = Annotated[JobApi, Depends(get_job_api)]
Fabric = Annotated[FabricView, Depends(get_fabric)]


def request_hash(
    manifest: dict[str, Any], bundle_digest: str, placement: Placement
) -> str:
    """SHA-256 of the canonical request: what "the same request" means for a key."""
    canonical = json.dumps(
        {
            "manifest": manifest,
            "bundle_digest": bundle_digest,
            "placement": placement.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_key(request: Request) -> str | None:
    values = request.headers.getlist("idempotency-key")
    if not values:
        return None
    if len(values) > 1 or not _KEY_RE.fullmatch(values[0]):
        raise ApiError.invalid(
            "Idempotency-Key must be one header of 1-128 printable ASCII characters "
            "without spaces"
        )
    return values[0]


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(
            FailureType.INVALID_INPUT,
            f"request body larger than {limit} bytes",
            http_status=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(
                FailureType.INVALID_INPUT,
                f"request body larger than {limit} bytes",
                http_status=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_submission(body: bytes) -> JobSubmission:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise ApiError.invalid("the request body is not valid JSON") from None
    try:
        return JobSubmission.model_validate(data)
    except ValidationError as exc:
        where = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ())) or '<body>'}: "
            f"{err.get('msg', 'invalid')}"
            for err in exc.errors(include_input=False, include_url=False)[:5]
        )
        raise ApiError.invalid(f"invalid job request: {where}") from None


def _summary_fields(job: JobRecord) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "name": job.name,
        "state": job.state,
        "bundle_digest": job.bundle_digest,
        "placement": Placement.model_validate(dict(job.placement)),
        "failure_type": job.failure_type,
        "failure_detail": job.failure_detail,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _attempt(record: AttemptRecord) -> AttemptSummary:
    return AttemptSummary(
        attempt_id=record.id,
        node_id=record.node_id,
        fencing_token=record.fencing_token,
        state=record.state,
        failure_type=record.failure_type,
        detail=record.detail,
        exit_code=record.exit_code,
        output_digest=record.output_digest,
        started_at=record.started_at,
        ended_at=record.ended_at,
    )


async def _detail(store: JobStore, job: JobRecord) -> JobDetail:
    transitions = await store.transitions(job.id)
    attempts = await store.list_attempts(job.id)
    return JobDetail(
        **_summary_fields(job),
        manifest=dict(job.manifest),
        transitions=tuple(
            TransitionOut(
                seq=t.seq, from_state=t.from_state, to_state=t.to_state, at=t.at
            )
            for t in transitions
        ),
        attempts=tuple(_attempt(a) for a in attempts),
    )


def _replay(existing: JobRecord, digest: str, response: Response) -> JobCreated:
    if not hmac.compare_digest(existing.request_hash, digest):
        raise ApiError(
            FailureType.CONFLICT,
            "this Idempotency-Key was already used for a different request",
        )
    response.status_code = status.HTTP_200_OK
    log.info("controller.jobs.idempotent_replay", job_id=existing.id)
    return JobCreated(job_id=existing.id, state=existing.state, created=False)


@job_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=JobCreated,
    summary="Submit a job (manual placement; it stays queued until dispatch lands)",
)
async def submit_job(
    request: Request,
    response: Response,
    _auth: FabricTokenRequired,
    jobs: Jobs,
    fabric: Fabric,
) -> JobCreated:
    key = _idempotency_key(request)
    submission = _parse_submission(await _read_body(request, jobs.max_request_bytes))
    try:
        manifest = parse_manifest_data(submission.manifest)
    except ManifestError as exc:
        raise ApiError.invalid(exc.message) from None
    try:
        node_id = str(validate_node_id(submission.placement.node_id))
    except InvalidIdError:
        raise ApiError.invalid(
            "placement.node_id is not a canonical node_<ULID>"
        ) from None
    placement = Placement(
        mode="local" if submission.placement.local else "offload", node_id=node_id
    )
    canonical_manifest = manifest.canonical_dict()
    digest = request_hash(canonical_manifest, submission.bundle_digest, placement)

    if key is not None:
        existing = await jobs.store.get_job_by_key(key)
        if existing is not None:
            return _replay(existing, digest, response)

    bundle_hex = submission.bundle_digest.removeprefix("sha256:")
    if jobs.blobs.size(Digest(bundle_hex)) is None:
        raise ApiError(
            FailureType.CONFLICT,
            f"bundle {submission.bundle_digest} is not in the blob store; upload it "
            f"with PUT /v1/blobs/{bundle_hex} first",
        )
    node = await fabric.registry.get_node(node_id)
    try:
        check_placement(
            manifest, node, connected=fabric.manager.get(node_id) is not None
        )
    except PlacementError as exc:
        log.info(
            "controller.jobs.placement_refused",
            node_id=node_id,
            failure_type=exc.failure_type.value,
            reason=exc.message,
        )
        if exc.failure_type is FailureType.NOT_FOUND:
            raise ApiError.not_found("node") from None
        raise ApiError(exc.failure_type, exc.message) from None

    try:
        job, created = await jobs.store.create_job(
            job_id=str(new_job_id()),
            name=manifest.name,
            manifest=canonical_manifest,
            bundle_digest=submission.bundle_digest,
            placement=placement.model_dump(mode="json"),
            request_hash=digest,
            idempotency_key=key,
        )
    except IdempotencyConflictError:
        raise ApiError(
            FailureType.CONFLICT,
            "this Idempotency-Key was already used for a different request",
        ) from None
    if not created:  # lost a concurrent race for the same key, same request
        return _replay(job, digest, response)
    await jobs.store.transition_job(job.id, JobEvent.PLAN, expected=JobState.SUBMITTED)
    job = await jobs.store.transition_job(
        job.id, JobEvent.PLANNED, expected=JobState.PLANNING
    )
    log.info(
        "controller.jobs.submitted",
        job_id=job.id,
        node_id=node_id,
        mode=placement.mode,
        bundle_digest=submission.bundle_digest,
        idempotency_key_set=key is not None,
    )
    return JobCreated(job_id=job.id, state=job.state, created=True)


@job_router.get("", response_model=JobList, summary="Recent jobs, newest first")
async def list_jobs(
    _auth: FabricTokenRequired,
    jobs: Jobs,
    limit: Annotated[
        int, Query(ge=1, le=MAX_JOB_LIMIT, description="Jobs returned, newest first.")
    ] = DEFAULT_JOB_LIMIT,
    state: Annotated[
        JobState | None, Query(description="Only jobs in this state.")
    ] = None,
) -> JobList:
    records = await jobs.store.list_jobs(limit=limit, state=state)
    summaries = tuple(JobSummary(**_summary_fields(j)) for j in records)
    return JobList(jobs=summaries, count=len(summaries))


@job_router.get("/{job_id}", response_model=JobDetail, summary="One job")
async def get_job(
    _auth: FabricTokenRequired,
    jobs: Jobs,
    job_id: Annotated[str, Path(max_length=_JOB_ID_MAX)],
) -> JobDetail:
    try:
        checked = str(validate_job_id(job_id))
    except InvalidIdError:
        raise ApiError.invalid("job_id is not a canonical job_<ULID>") from None
    job = await jobs.store.get_job(checked)
    if job is None:
        raise ApiError.not_found("job")
    return await _detail(jobs.store, job)
