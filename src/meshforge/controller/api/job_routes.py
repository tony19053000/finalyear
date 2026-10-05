"""``POST /v1/jobs``, ``GET /v1/jobs``, ``GET /v1/jobs/{job_id}`` (brief D50).

Every route needs the fabric token (D44: job reads expose user output).

**Submit** ``{manifest, tree_id | bundle_digest, placement: {node_id, local?}}``
with an optional ``Idempotency-Key`` header (1-128 printable ASCII characters).
``tree_id`` (V0.5 D83) names a tree manifest already uploaded with have/need
(``POST /v1/blobs/have``); ``bundle_digest`` is the V0.4 single-bundle path, kept
for existing callers:

1. the body is read with a hard size cap and validated (shape, then the manifest
   with the same model the CLI and agent use - errors carry YAML paths);
2. a key already used with the **same** canonical request returns that job
   (``200``, ``created: false``: no second job, no second execution); with a
   **different** request it is ``409 conflict``;
3. the bundle must already be in the blob store (``409 conflict`` naming the
   digest - upload it with ``PUT /v1/blobs/{sha256}`` first); for a tree, the
   manifest must be stored and valid (``409`` absent, ``400`` not a valid tree)
   and so must every blob it lists (``409`` naming up to 20 missing digests);
4. placement is checked (V0.6 D103/D104). **Pinned** (``placement.node_id``):
   ``404`` unknown node; ``409 no_feasible_plan`` for a node that is not online,
   has no live session, does not accept jobs, or cannot give the job the
   GPU/VRAM/RAM/CPUs/runtime/slot kind it asks for. **Auto** (no ``node_id``):
   at least one enrolled node must pass the planner's *static* rules (liveness
   and load ignored - a busy fabric makes the job wait, it does not refuse it);
   otherwise ``409 no_feasible_plan`` with ``error.rejections`` = every node's
   ``{node_id, reject_code, reject_text}``;
5. only then is the job written: ``SUBMITTED -> PLANNING -> QUEUED``, each arrow
   recorded with its time (``PLANNING`` is a pass-through: the live planner runs
   at dispatch) - the row, its scheduling fields (priority, policy, retry,
   submitter, project bytes, workload fingerprint) and all three arrows commit
   in **one** transaction
   (:meth:`JobStore.create_queued_job`), so no job is ever left half-submitted
   and a replay always sees it ``queued`` or later. Answer
   ``201 {job_id, state: "queued", created: true}``.

**A rejected request never creates a row.** Concurrent duplicate POSTs with one
key produce one row (the unique index), and every loser gets the winner's job.

**Dispatch (V0.4 G4, D65; V0.6 G2):** an accepted job wakes the dispatcher,
whose planner places it (or, pinned, starts it on its node) as soon as a node
can take it; every decision is recorded (``GET /v1/jobs/{id}/decisions``).

**Cancel** ``POST /v1/jobs/{job_id}/cancel`` (D69, idempotent): a job without a
live attempt (queued, planning, retrying) is ``cancelled`` at once; a job whose
attempt runs gets ``CancelAttempt`` and becomes ``cancelled`` when the agent
reports it stopped; ``202`` either way. A job that already ended is ``200`` and
unchanged.
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import ValidationError

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_job_id, validate_job_id, validate_node_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import (
    Manifest,
    ManifestError,
    parse_manifest_data,
)
from meshforge.common.states import JOB_TERMINAL_STATES, JobState
from meshforge.common.tree import Tree, TreeError
from meshforge.controller.api.auth import FabricTokenRequired
from meshforge.controller.api.context import FabricView, get_fabric
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_models import (
    AttemptSummary,
    FinalLogSummary,
    InputOut,
    JobCancelled,
    JobCreated,
    JobDetail,
    JobList,
    JobSubmission,
    JobSummary,
    Placement,
    TransferOut,
    TransitionOut,
)
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.env_health import env_key
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.placement import (
    PlacementError,
    check_auto,
    check_placement,
)
from meshforge.controller.jobs.queue_reads import MAX_NODES as MAX_PLANNED_NODES
from meshforge.controller.jobs.store import (
    AttemptRecord,
    IdempotencyConflictError,
    JobRecord,
    JobScheduling,
    JobStore,
)
from meshforge.controller.named_inputs import (
    NamedInputError,
    NamedInputStore,
    PinBroadcaster,
    ResolvedInput,
)
from meshforge.controller.planner.inputs import workload_fingerprint
from meshforge.controller.prepare import PrepareTracker

__all__ = [
    "DEFAULT_JOB_LIMIT",
    "IDEMPOTENCY_KEY_MAX_CHARS",
    "JOBS_STATE_KEY",
    "MAX_JOB_LIMIT",
    "JobApi",
    "check_tree",
    "get_job_api",
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
_MISSING_SHOWN: Final = 20


class JobApi:
    """What the job routes need; put on ``app.state`` by ``create_http_app``."""

    def __init__(
        self,
        *,
        store: JobStore,
        blobs: BlobStore,
        max_request_bytes: int,
        dispatcher: Dispatcher | None = None,
        logs: LogHub | None = None,
        inputs: NamedInputStore | None = None,
        pins: PinBroadcaster | None = None,
        prepare: PrepareTracker | None = None,
    ) -> None:
        self.store = store
        self.prepare = prepare
        """V0.5 G5 (0.5.8): `compute node prepare` requests; None = off."""
        self.inputs = inputs
        """V0.5 G5 (D92): named inputs; None = /v1/data is off, inputs refused."""
        self.pins = pins
        self.blobs = blobs
        self.max_request_bytes = max_request_bytes
        self.logs = logs
        """D71/D72 log files and followers; None = logs are not served."""
        self.dispatcher = dispatcher
        """None in tests that mount the routes without a running controller: a
        job is then stored and stays queued, and cancel is refused."""


def get_job_api(request: Request) -> JobApi:
    api = getattr(request.app.state, JOBS_STATE_KEY, None)
    if not isinstance(api, JobApi):
        raise ApiError(FailureType.CONFIG_ERROR, "the job API is not configured")
    return api


job_router = APIRouter(prefix="/v1/jobs", tags=["jobs"])
Jobs = Annotated[JobApi, Depends(get_job_api)]
Fabric = Annotated[FabricView, Depends(get_fabric)]


def request_hash(
    manifest: dict[str, Any],
    bundle_digest: str | None,
    placement: Placement,
    *,
    tree_id: str | None = None,
    submitter: tuple[str, bool] | None = None,
) -> str:
    """SHA-256 of the canonical request: what "the same request" means for a key.

    A bundle request hashes exactly as in V0.4; a tree request names ``tree_id``.
    V0.6 fields (an auto placement's absent ``node_id``, ``policy``, the
    submitter) only appear when set, so a V0.5 request keeps its hash.
    """
    source = (
        {"tree_id": tree_id}
        if tree_id is not None
        else {"bundle_digest": bundle_digest}
    )
    extra = (
        {}
        if submitter is None
        else {"submitter": {"node_id": submitter[0], "seeded": submitter[1]}}
    )
    canonical = json.dumps(
        {
            "manifest": manifest,
            **source,
            "placement": placement.model_dump(mode="json", exclude_none=True),
            **extra,
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
        "tree_id": job.tree_id,
        "placement": Placement.model_validate(dict(job.placement)),
        "failure_type": job.failure_type,
        "failure_detail": job.failure_detail,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "priority": job.scheduling.priority,
        "submitter_node_id": job.scheduling.submitter_node_id,
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
        missing_outputs=list(record.missing_outputs),
        logs={
            stream: FinalLogSummary.model_validate(info)
            for stream, info in (record.logs or {}).items()
            if stream in ("stdout", "stderr") and isinstance(info, dict)
        },
        started_at=record.started_at,
        ended_at=record.ended_at,
    )


async def _detail(
    store: JobStore, job: JobRecord, inputs: NamedInputStore | None = None
) -> JobDetail:
    transitions = await store.transitions(job.id)
    attempts = await store.list_attempts(job.id)
    by_attempt: dict[str, list[TransferOut]] = {}
    for attempt_id, row in await store.transfers(job.id):
        by_attempt.setdefault(attempt_id, []).append(
            TransferOut(
                tree_id=row.tree_id,
                source=row.source,
                bytes=row.bytes,
                duration_ms=row.duration_ms,
                units=row.units,
                failures=row.failures,
                resumed_chunks=row.resumed_chunks,
                dropped=row.dropped,
            )
        )
    used = [] if inputs is None else await inputs.job_inputs(job.id)
    return JobDetail(
        **_summary_fields(job),
        manifest=dict(job.manifest),
        transitions=tuple(
            TransitionOut(
                seq=t.seq, from_state=t.from_state, to_state=t.to_state, at=t.at
            )
            for t in transitions
        ),
        attempts=tuple(
            _attempt(a).model_copy(update={"transfers": by_attempt.get(a.id, [])})
            for a in attempts
        ),
        inputs=tuple(
            InputOut(
                ref=i.ref,
                name=i.name,
                version=i.version,
                tree_id=i.tree_id,
                mount=i.mount,
            )
            for i in used
        ),
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


async def check_tree(blobs: BlobStore, tree_id: Digest) -> Tree:
    """The tree manifest and every blob it lists are stored (D82/D83)."""
    try:
        tree = await blobs.tree(tree_id)
    except TreeError as exc:
        raise ApiError.invalid(
            f"tree {tree_id} is not a valid tree: {exc.message}"
        ) from None
    if tree is None:
        raise ApiError(
            FailureType.CONFLICT,
            f"tree {tree_id} is not in the blob store; upload the manifest with "
            f"PUT /v1/blobs/{tree_id.hex} first",
        )
    missing = blobs.missing(Digest(h) for h in tree.blob_sizes())
    if missing:
        shown = ", ".join(str(d) for d in missing[:_MISSING_SHOWN])
        more = len(missing) - _MISSING_SHOWN
        raise ApiError(
            FailureType.CONFLICT,
            f"{len(missing)} blob(s) of tree {tree_id} are not in the blob store "
            f"(ask POST /v1/blobs/have, upload them, retry): {shown}"
            + (f" and {more} more" if more > 0 else ""),
        )
    return tree


def _mount_conflict(project: Tree, mounts: Sequence[str]) -> tuple[str, str] | None:
    """``(mount, project path)`` of the first overlap, else None. O(inputs x
    (depth + log n)): a project path equal to the mount or one of its parent
    folders (a set), or any project path under the mount (bisect on the sorted
    paths). Blocking for a large tree: run it in a thread."""
    paths = sorted(entry.path for entry in project.entries)
    known = set(paths)
    for mount in mounts:
        parts = mount.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            if prefix in known:
                return mount, prefix
        below = mount + "/"
        index = bisect.bisect_left(paths, below)
        if index < len(paths) and paths[index].startswith(below):
            return mount, paths[index]
    return None


async def _check_mounts(project: Tree | None, inputs: Sequence[ResolvedInput]) -> None:
    """V0.5 D92: an input mount must not hide or mix with project files."""
    if project is None:
        if inputs:
            raise ApiError.invalid(
                "named inputs need a tree job (compute run sends one)"
            )
        return
    if not inputs:
        return
    conflict = await asyncio.to_thread(
        _mount_conflict, project, [item.mount for item in inputs]
    )
    if conflict is not None:
        mount, path = conflict
        raise ApiError.invalid(
            f"input mount {mount!r} overlaps the project path {path!r}; choose a "
            "folder the project does not use"
        )


def _node_id_or_none(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        return str(validate_node_id(value))
    except InvalidIdError:
        raise ApiError.invalid(f"{field} is not a canonical node_<ULID>") from None


async def _check_where(
    fabric: FabricView, manifest: Manifest, node_id: str | None
) -> None:
    """Pinned: the V0.4 check of that one node. Auto (D104): at least one
    enrolled node could ever run it; otherwise 409 with every rejection."""
    try:
        if node_id is not None:
            node = await fabric.registry.get_node(node_id)
            check_placement(
                manifest, node, connected=fabric.manager.get(node_id) is not None
            )
        else:
            nodes = await fabric.registry.list_nodes(limit=MAX_PLANNED_NODES)
            check_auto(manifest, nodes)
    except PlacementError as exc:
        # No job_id on purpose: a refused request creates no job (D50, D62-5).
        log.info(
            "controller.jobs.placement_refused",
            node_id=node_id,
            failure_type=exc.failure_type.value,
            reason=exc.message,
        )
        if exc.failure_type is FailureType.NOT_FOUND:
            raise ApiError.not_found("node") from None
        rejections = exc.details.get("rejections")
        raise ApiError(
            exc.failure_type,
            exc.message,
            extra={"rejections": rejections} if rejections is not None else None,
        ) from None


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
    node_id = _node_id_or_none(submission.placement.node_id, "placement.node_id")
    submitter = _node_id_or_none(submission.submitter_node_id, "submitter_node_id")
    if node_id is None:
        mode: Literal["local", "offload", "auto"] = "auto"
    else:
        mode = "local" if submission.placement.local else "offload"
    placement = Placement(
        mode=mode, node_id=node_id, policy=submission.placement.policy
    )
    canonical_manifest = manifest.canonical_dict()
    try:
        json.dumps(canonical_manifest, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError.invalid(
            "the manifest contains text that is not valid UTF-8"
        ) from None
    digest = request_hash(
        canonical_manifest,
        submission.bundle_digest,
        placement,
        tree_id=submission.tree_id,
        submitter=None if submitter is None else (submitter, submission.seeded),
    )

    if key is not None:
        existing = await jobs.store.get_job_by_key(key)
        if existing is not None:
            return _replay(existing, digest, response)

    # V0.5 G5: from the blob check to the job row, no GC pass may remove a blob
    # this job references (the pass holds the same guard).
    async with jobs.blobs.gc_guard.shared():
        resolved: list[ResolvedInput] = []
        if manifest.inputs:
            if jobs.inputs is None:
                raise ApiError(
                    FailureType.CONFIG_ERROR, "named inputs are not configured"
                )
            try:
                resolved = await jobs.inputs.resolve(manifest.inputs)
            except NamedInputError as exc:
                raise ApiError(exc.failure_type, exc.message) from None
        project: Tree | None = None
        if submission.tree_id is not None:
            project = await check_tree(jobs.blobs, Digest.parse(submission.tree_id))
        await _check_mounts(project, resolved)
        for item in resolved:
            await check_tree(jobs.blobs, Digest.parse(item.tree_id))
        if submission.tree_id is None:
            bundle_hex = (submission.bundle_digest or "").removeprefix("sha256:")
            if jobs.blobs.size(Digest(bundle_hex)) is None:
                raise ApiError(
                    FailureType.CONFLICT,
                    f"bundle {submission.bundle_digest} is not in the blob store; "
                    f"upload it with PUT /v1/blobs/{bundle_hex} first",
                )
        await _check_where(fabric, manifest, node_id)
        project_key = submission.tree_id or submission.bundle_digest or ""
        bundle_hex = (submission.bundle_digest or "").removeprefix("sha256:")
        request_env = env_key(
            canonical_manifest,
            tree_id=submission.tree_id,
            bundle_digest=submission.bundle_digest,
        )
        scheduling = JobScheduling(
            priority=manifest.priority,
            policy=submission.placement.policy or manifest.policy,
            max_attempts=manifest.retry.max_attempts,
            retry_backoff_s=manifest.retry.backoff_s,
            on_lost_retry=manifest.retry.on_lost,
            submitter_node_id=submitter,
            submitter_seeded=submission.seeded and submitter is not None,
            input_bytes=(
                sum(project.blob_sizes().values())
                if project is not None
                else jobs.blobs.size(Digest(bundle_hex))
            ),
            fingerprint=workload_fingerprint(
                manifest, project=project_key, env_key=request_env
            ),
        )
        try:
            job, created = await jobs.store.create_queued_job(
                job_id=str(new_job_id()),
                name=manifest.name,
                manifest=canonical_manifest,
                bundle_digest=submission.bundle_digest,
                placement=placement.model_dump(mode="json", exclude_none=True),
                request_hash=digest,
                idempotency_key=key,
                tree_id=submission.tree_id,
                inputs=[
                    (i.mount, i.ref, i.name, i.version, i.tree_id) for i in resolved
                ],
                scheduling=scheduling,
            )
        except IdempotencyConflictError:
            raise ApiError(
                FailureType.CONFLICT,
                "this Idempotency-Key was already used for a different request",
            ) from None
    if not created:  # lost a concurrent race for the same key, same request
        return _replay(job, digest, response)
    log.info(
        "controller.jobs.submitted",
        job_id=job.id,
        node_id=node_id,
        mode=placement.mode,
        priority=manifest.priority,
        policy=scheduling.policy,
        submitter_node_id=submitter,
        bundle_digest=submission.bundle_digest,
        tree_id=submission.tree_id,
        idempotency_key_set=key is not None,
    )
    if jobs.dispatcher is not None:
        jobs.dispatcher.wake()
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
    return await _detail(jobs.store, job, jobs.inputs)


@job_router.post(
    "/{job_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCancelled,
    summary="Cancel a job (idempotent; 202 accepted, 200 already ended)",
)
async def cancel_job(
    response: Response,
    _auth: FabricTokenRequired,
    jobs: Jobs,
    job_id: Annotated[str, Path(max_length=_JOB_ID_MAX)],
) -> JobCancelled:
    try:
        checked = str(validate_job_id(job_id))
    except InvalidIdError:
        raise ApiError.invalid("job_id is not a canonical job_<ULID>") from None
    if jobs.dispatcher is None:
        raise ApiError(FailureType.CONFIG_ERROR, "this controller does not dispatch")
    before = await jobs.store.get_job(checked)
    if before is None:
        raise ApiError.not_found("job")
    if before.state in JOB_TERMINAL_STATES:
        response.status_code = status.HTTP_200_OK
        return JobCancelled(job_id=checked, state=before.state, cancel_requested=False)
    job = await jobs.dispatcher.cancel(checked)
    if job is None:  # pragma: no cover - the row cannot vanish (no delete path)
        raise ApiError.not_found("job")
    log.info(
        "controller.jobs.cancel_requested",
        job_id=checked,
        from_state=before.state.value,
        state=job.state.value,
    )
    return JobCancelled(
        job_id=checked,
        state=job.state,
        cancel_requested=job.state not in JOB_TERMINAL_STATES,
    )
