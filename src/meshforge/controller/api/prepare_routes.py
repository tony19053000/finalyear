"""REST for ``compute node prepare`` (V0.5 G5, roadmap 0.5.8). Fabric token.

* ``POST /v1/nodes/{node_id}/prepare`` ``{images: [base...], envs: [{base,
  python?, requirements (the file's text), runtime?}]}`` -> ``202 {request_id}``;
  ``409`` when the node is not connected; bounded (8 images, 32 envs, each
  requirements text <= 64 KiB, body <= 2.5 MiB).
* ``POST /v1/nodes/{node_id}/gc`` -> ``202``: the node runs a GC pass now
  (``GcRequest{run_now}``, V0.5 G5 D91); ``409`` when it is not connected.
* ``GET /v1/nodes/{node_id}/prepare/{request_id}`` -> the request's items
  (``pending`` / ``running`` / ``hit`` / ``pulled`` / ``built`` / ``failed``
  with a typed reason) and ``done`` / ``ok``; ``404`` unknown (or forgotten
  by a controller restart).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Request, status

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.api.auth import FabricTokenRequired
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_routes import JobApi, get_job_api
from meshforge.controller.prepare import PrepareTracker

__all__ = ["prepare_router"]

MAX_IMAGES: Final = 8
MAX_ENVS: Final = 32
MAX_REQUIREMENTS_CHARS: Final = 65_536
MAX_BODY: Final = 2_621_440
_BASES: Final = ("python-cpu", "cuda-pytorch")
_RUNTIMES: Final = ("", "docker", "venv", "subprocess")
_PYTHON_RE: Final = re.compile(r"3\.\d{1,2}")

prepare_router = APIRouter(prefix="/v1/nodes", tags=["nodes"])
Jobs = Annotated[JobApi, Depends(get_job_api)]
NodeId = Annotated[str, Path(max_length=40)]
RequestId = Annotated[str, Path(max_length=64)]


def _tracker(jobs: JobApi) -> PrepareTracker:
    if jobs.prepare is None:
        raise ApiError(FailureType.CONFIG_ERROR, "node prepare is not configured")
    return jobs.prepare


def _node(node_id: str) -> str:
    try:
        return str(validate_node_id(node_id))
    except InvalidIdError:
        raise ApiError.invalid("node_id is not a canonical node_<ULID>") from None


async def _body(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            raise ApiError(
                FailureType.INVALID_INPUT,
                f"request body larger than {MAX_BODY} bytes",
                http_status=status.HTTP_413_CONTENT_TOO_LARGE,
            )
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, RecursionError):
        raise ApiError.invalid("the request body is not valid JSON") from None
    if not isinstance(body, dict) or not set(body) <= {"images", "envs"}:
        raise ApiError.invalid('the body is {"images": [...], "envs": [...]}')
    return body


def _envs(raw: object) -> list[agent_pb2.EnvSpecRequest]:
    if not isinstance(raw, list) or len(raw) > MAX_ENVS:
        raise ApiError.invalid(f"envs is a list of at most {MAX_ENVS}")
    out: list[agent_pb2.EnvSpecRequest] = []
    for env in raw:
        if not isinstance(env, dict) or not set(env) <= {
            "base",
            "python",
            "requirements",
            "runtime",
        }:
            raise ApiError.invalid("an env is {base, python?, requirements, runtime?}")
        base = env.get("base", "python-cpu")
        python = env.get("python") or ""
        text = env.get("requirements", "")
        runtime = env.get("runtime") or ""
        if base not in _BASES:
            raise ApiError.invalid(f"env base must be one of {', '.join(_BASES)}")
        if not isinstance(python, str) or (python and not _PYTHON_RE.fullmatch(python)):
            raise ApiError.invalid('env python must look like "3.12"')
        if not isinstance(text, str) or len(text) > MAX_REQUIREMENTS_CHARS:
            raise ApiError.invalid(
                f"env requirements is text of at most {MAX_REQUIREMENTS_CHARS} characters"
            )
        if runtime not in _RUNTIMES:
            raise ApiError.invalid("env runtime must be docker, venv or subprocess")
        out.append(
            agent_pb2.EnvSpecRequest(
                base=base,
                python=python,
                requirements=text.encode("utf-8"),
                runtime=runtime,
            )
        )
    return out


@prepare_router.post(
    "/{node_id}/prepare",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Pull base images / build environments on a node",
)
async def start_prepare(
    request: Request, node_id: NodeId, _auth: FabricTokenRequired, jobs: Jobs
) -> dict[str, Any]:
    tracker = _tracker(jobs)
    checked = _node(node_id)
    body = await _body(request)
    images = body.get("images", ["python-cpu"])
    if (
        not isinstance(images, list)
        or len(images) > MAX_IMAGES
        or not all(i in _BASES for i in images)
    ):
        raise ApiError.invalid(f"images is a list of base names ({', '.join(_BASES)})")
    envs = _envs(body.get("envs", []))
    record = tracker.start(checked, images=list(dict.fromkeys(images)), envs=envs)
    if record is None:
        raise ApiError(
            FailureType.CONFLICT, "the node is not connected; start its agent first"
        )
    return {"request_id": record.request_id, "node_id": checked}


@prepare_router.get(
    "/{node_id}/prepare/{request_id}", summary="Progress of a prepare request"
)
async def prepare_status(
    node_id: NodeId, request_id: RequestId, _auth: FabricTokenRequired, jobs: Jobs
) -> dict[str, Any]:
    record = _tracker(jobs).get(_node(node_id), request_id)
    if record is None:
        raise ApiError.not_found("prepare request")
    return record.as_dict()


@prepare_router.post(
    "/{node_id}/gc",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask a node to run a GC pass now",
)
async def node_gc(
    node_id: NodeId, _auth: FabricTokenRequired, jobs: Jobs
) -> dict[str, Any]:
    tracker = _tracker(jobs)
    checked = _node(node_id)
    if not tracker.request_gc(checked):
        raise ApiError(
            FailureType.CONFLICT, "the node is not connected; start its agent first"
        )
    return {"node_id": checked, "requested": True}
