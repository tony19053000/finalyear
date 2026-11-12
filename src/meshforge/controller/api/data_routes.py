"""REST for named inputs (V0.5 G5, D92): ``/v1/data``. Any user (V0.7 G4,
D141): named inputs are shared by the whole fabric.

* ``POST /v1/data`` ``{name, tree_id, pinned?}`` - register an uploaded tree
  (its manifest and every blob must be stored, exactly like a job submission):
  ``201`` a new version, ``200`` the version that already names this tree.
* ``GET /v1/data`` - every version of every input (at most 1000).
* ``GET /v1/data/{name}`` - one input's versions (``404`` if none).
* ``PATCH /v1/data/{name}/{version}`` ``{pinned}`` - pin or unpin a version.
* ``DELETE /v1/data/{name}/{version}`` - ``409`` (naming the jobs) while an
  unfinished job uses it, ``404`` unknown, else ``200`` with what was removed.
  V1.0 G11 (D454): the removed tree's blobs that nothing else references are
  unlinked at once (``freed_blobs`` / ``freed_bytes``), not at quota pressure.
* ``POST /v1/data/gc?dry_run=`` (admin, V1.0 G11, D454) - sweep every
  unreferenced blob of the controller's store now; answers count and bytes.

A change to the pinned set is sent to every live agent at once.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.manifest import INPUT_NAME_RE
from meshforge.controller.api.auth import AdminUser, CurrentUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_routes import JobApi, check_tree, get_job_api
from meshforge.controller.api.response_models import (
    DataGcOut,
    InputList,
    InputOut,
    InputRegistered,
    InputRemoved,
    InputVersions,
)
from meshforge.controller.named_inputs import (
    NamedInputError,
    NamedInputRecord,
    NamedInputStore,
)

__all__ = ["data_router"]

log = get_logger(__name__)

_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_BODY: Final = 4096
_MAX_VERSION: Final = 999_999_999

data_router = APIRouter(prefix="/v1/data", tags=["data"])
Jobs = Annotated[JobApi, Depends(get_job_api)]
Name = Annotated[str, Path(min_length=1, max_length=64)]
Version = Annotated[int, Path(ge=1, le=_MAX_VERSION)]


def _store(jobs: JobApi) -> NamedInputStore:
    if jobs.inputs is None:
        raise ApiError(FailureType.CONFIG_ERROR, "named inputs are not configured")
    return jobs.inputs


def _name(value: str) -> str:
    if not INPUT_NAME_RE.fullmatch(value):
        raise ApiError.invalid(
            "an input name is 1-64 lower-case letters, digits, '.', '_', '-' "
            "(starting with a letter or digit)"
        )
    return value


async def _json_body(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > _MAX_BODY:
            raise ApiError(
                FailureType.INVALID_INPUT,
                f"request body larger than {_MAX_BODY} bytes",
                http_status=status.HTTP_413_CONTENT_TOO_LARGE,
            )
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise ApiError.invalid("the request body is not valid JSON") from None
    if not isinstance(body, dict):
        raise ApiError.invalid("the request body must be a JSON object")
    return body


def _error(exc: NamedInputError) -> ApiError:
    if exc.failure_type is FailureType.NOT_FOUND:
        return ApiError(FailureType.NOT_FOUND, exc.message)
    return ApiError(exc.failure_type, exc.message)


async def _pins_changed(jobs: JobApi) -> None:
    if jobs.pins is not None:
        await jobs.pins.refresh(broadcast=True)


@data_router.post(
    "",
    summary="Register an uploaded tree as a named input",
    response_model=InputRegistered,
)
async def push_input(
    request: Request, response: Response, _user: CurrentUser, jobs: Jobs
) -> dict[str, Any]:
    store = _store(jobs)
    body = await _json_body(request)
    if not set(body) <= {"name", "tree_id", "pinned"} or not {"name", "tree_id"} <= set(
        body
    ):
        raise ApiError.invalid('the body is {"name", "tree_id", "pinned"?}')
    name, tree_id, pinned = body["name"], body["tree_id"], body.get("pinned", False)
    if not isinstance(name, str) or not isinstance(tree_id, str):
        raise ApiError.invalid("name and tree_id are strings")
    if not isinstance(pinned, bool):
        raise ApiError.invalid("pinned is true or false")
    _name(name)
    if not _DIGEST_RE.fullmatch(tree_id):
        raise ApiError.invalid("tree_id must be sha256:<64 lower-case hex>")
    async with jobs.blobs.gc_guard.shared():
        tree = await check_tree(jobs.blobs, Digest.parse(tree_id))
        files = list(tree.files())
        try:
            record, created = await store.push(
                name,
                tree_id,
                size=sum(e.size for e in files),
                files=len(files),
                pinned=pinned,
            )
        except NamedInputError as exc:
            raise _error(exc) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED
    if pinned:
        await _pins_changed(jobs)
    return {**record.as_dict(), "created": created}


@data_router.get("", summary="Every named input version", response_model=InputList)
async def list_inputs(_user: CurrentUser, jobs: Jobs) -> dict[str, Any]:
    records = await _store(jobs).list_all()
    return {"inputs": [r.as_dict() for r in records], "count": len(records)}


@data_router.get(
    "/{name}", summary="One named input's versions", response_model=InputVersions
)
async def show_input(name: Name, _user: CurrentUser, jobs: Jobs) -> dict[str, Any]:
    records = await _store(jobs).versions(_name(name))
    if not records:
        raise ApiError.not_found("input")
    return {"name": name, "versions": [r.as_dict() for r in records]}


@data_router.patch(
    "/{name}/{version}", summary="Pin or unpin one version", response_model=InputOut
)
async def pin_input(
    request: Request,
    name: Name,
    version: Version,
    _user: CurrentUser,
    jobs: Jobs,
) -> dict[str, Any]:
    body = await _json_body(request)
    pinned = body.get("pinned")
    if set(body) != {"pinned"} or not isinstance(pinned, bool):
        raise ApiError.invalid('the body is {"pinned": true|false}')
    record = await _store(jobs).set_pinned(_name(name), version, pinned)
    if record is None:
        raise ApiError.not_found("input")
    await _pins_changed(jobs)
    return record.as_dict()


@data_router.delete(
    "/{name}/{version}", summary="Remove one version", response_model=InputRemoved
)
async def remove_input(
    name: Name, version: Version, _user: CurrentUser, jobs: Jobs
) -> dict[str, Any]:
    try:
        record: NamedInputRecord = await _store(jobs).remove(_name(name), version)
    except NamedInputError as exc:
        raise _error(exc) from None
    if record.pinned:
        await _pins_changed(jobs)
    return {**record.as_dict(), "removed": True, **await _sweep_removed(jobs, record)}


async def _sweep_removed(jobs: JobApi, record: NamedInputRecord) -> dict[str, Any]:
    """D454: unlink the removed tree's blobs nothing else references. The row
    is gone already (committed); a failed sweep only leaves them to GC."""
    if jobs.gc is None:
        return {"swept": False}
    tree = Digest.parse(record.tree_id)
    try:
        among = {tree.hex, *await jobs.blobs.tree_members(tree)}
        report = await jobs.gc.sweep("data_rm", among=among)
    except Exception as exc:  # noqa: BLE001 - the removal itself succeeded
        log.warning(
            "controller.data.sweep_failed",
            ref=f"{record.name}@{record.version}",
            cause=type(exc).__name__,
            error=str(exc)[:200],
        )
        return {"swept": False}
    return {
        "swept": True,
        "freed_blobs": report.removed,
        "freed_bytes": report.removed_bytes,
        "kept_recent_blobs": report.kept_recent,
    }


@data_router.post(
    "/gc",
    summary="Sweep unreferenced blobs from the controller's store now (admin)",
    response_model=DataGcOut,
)
async def sweep_blobs(
    _admin: AdminUser,
    jobs: Jobs,
    dry_run: Annotated[bool, Query(description="Count only; unlink nothing.")] = False,
) -> dict[str, Any]:
    if jobs.gc is None:
        raise ApiError(
            FailureType.CONFIG_ERROR,
            "the controller's blob GC is off ([controller.gc] enabled = false, "
            "or its index is in use by another process)",
        )
    report = await jobs.gc.sweep("manual", dry_run=dry_run)
    return report.as_dict()
