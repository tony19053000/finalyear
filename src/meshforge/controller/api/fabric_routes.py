"""``/v1/fabric/invites`` - join codes over REST (V0.7 G2, D135, D151).

* ``POST /v1/fabric/invites`` ``{"ttl_s"?: int, "replace_node_id"?: str}`` ->
  201 with the code (shown once; only its hash is stored), the invite id, the
  address it names and its expiry;
* ``GET /v1/fabric/invites`` -> every invite, newest first, **never a code**;
* ``POST /v1/fabric/invites/{invite_id}/revoke`` -> the revoked invite (409
  when used or already revoked, 404 when unknown).

All three need the **fabric token** (D151: the interim admin credential until
V0.7 G4 re-gates them to ``role=admin`` - :func:`require_invite_admin` is that one
place). No fabric CA -> 409 ``conflict`` ("run fabric create"); an address the
controller cannot name -> 500 ``config_error`` with the setting to fix. The code
appears only in the 201 body; it is never logged (the scrubber also masks the
``mf1-`` shape).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, status
from pydantic import Field, ValidationError

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.ids import validate_node_id
from meshforge.controller.api.auth import require_fabric_token
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel
from meshforge.controller.enrollment.codes import (
    CodeAddressError,
    code_address,
    issue_code,
)
from meshforge.controller.enrollment.invites import (
    MAX_TTL_S,
    MIN_TTL_S,
    InviteConflictError,
    InviteError,
    InviteNotFoundError,
    InviteStore,
    InviteStoreError,
)
from meshforge.controller.fabric_ca import FabricCa
from meshforge.controller.settings import ControllerSettings

__all__ = ["FABRIC_STATE_KEY_INVITES", "InviteApi", "fabric_router"]

FABRIC_STATE_KEY_INVITES: Final = "meshforge_invites"
_ACTOR: Final = "rest:fabric-token"

fabric_router = APIRouter(prefix="/v1/fabric", tags=["fabric"])


class InviteApi:
    """What the routes need (on ``app.state``). ``fabric`` / ``enroll_port`` are
    read at call time: the CA can appear after start-up only by a restart, and
    the bound port is known only once the listener is up."""

    def __init__(
        self,
        *,
        store: InviteStore,
        settings: ControllerSettings,
        fabric: Callable[[], FabricCa | None],
        enroll_port: Callable[[], int],
    ) -> None:
        self.store = store
        self.settings = settings
        self.fabric = fabric
        self.enroll_port = enroll_port


def get_invite_api(request: Request) -> InviteApi:
    api: InviteApi | None = getattr(request.app.state, FABRIC_STATE_KEY_INVITES, None)
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "invites are not configured")
    return api


async def require_invite_admin(request: Request) -> None:
    """D151: who may invite. G2: the fabric token; G4 swaps in ``role=admin``."""
    await require_fabric_token(request)


InviteAdmin = Annotated[None, Depends(require_invite_admin)]


class InviteIn(ApiModel):
    ttl_s: int | None = Field(default=None, ge=MIN_TTL_S, le=MAX_TTL_S)
    replace_node_id: str | None = Field(default=None, max_length=40)


_MAX_BODY_BYTES: Final = 4096
_MAX_ID_CHARS: Final = 64


async def _invite_body(request: Request) -> InviteIn:
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise ApiError.invalid("the request body is too large")
    if not raw.strip():
        # No body at all = all defaults; a body *declared* JSON must be JSON.
        if "json" in request.headers.get("content-type", "").lower():
            raise ApiError.invalid("the request body is empty; send {} for defaults")
        return InviteIn()
    try:
        return InviteIn.model_validate_json(raw)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors()}
        )
        raise ApiError.invalid(
            f"invalid invite request ({', '.join(fields[:5])}): ttl_s must be "
            f"{MIN_TTL_S}-{MAX_TTL_S}, replace_node_id a node id"
        ) from None


def _source(request: Request) -> str | None:
    client = request.client
    return f"{client.host}:{client.port}" if client else None


def _api_error(exc: MeshForgeError) -> ApiError:
    if isinstance(exc, InviteNotFoundError):
        return ApiError.not_found("invite")
    if isinstance(exc, InviteConflictError):
        return ApiError(FailureType.CONFLICT, exc.message)
    if isinstance(exc, InviteError):
        return ApiError.invalid(exc.message)
    if isinstance(exc, InviteStoreError):
        return ApiError(FailureType.NETWORK_ERROR, "the database is unavailable")
    if isinstance(exc, CodeAddressError):
        return ApiError(FailureType.CONFIG_ERROR, exc.message)
    return ApiError(FailureType.INTERNAL_ERROR, "the invite could not be made")


@fabric_router.post("/invites", status_code=status.HTTP_201_CREATED)
async def create_invite(
    request: Request,
    _admin: InviteAdmin,
    api: Annotated[InviteApi, Depends(get_invite_api)],
) -> dict[str, Any]:
    # D152 (T-2): the body is read and validated only AFTER the fabric token was
    # checked (a declared body parameter would be validated first, letting an
    # unauthenticated caller probe the API's rules with 400s).
    body = await _invite_body(request)
    if body.replace_node_id is not None:
        try:
            validate_node_id(body.replace_node_id)
        except MeshForgeError:
            raise ApiError.invalid("replace_node_id must be a node id") from None
    fabric = api.fabric()
    if fabric is None:
        raise ApiError(
            FailureType.CONFLICT,
            "this controller has no fabric CA: run `meshforge-controller fabric "
            "create` on it, then restart it",
        )
    try:
        host, port = code_address(api.settings, bound_port=api.enroll_port())
        new = await api.store.create(
            ttl_s=body.ttl_s or api.settings.controller.enroll.invite_ttl_s,
            actor=_ACTOR,
            replace_node_id=body.replace_node_id,
            source=_source(request),
        )
    except MeshForgeError as exc:
        raise _api_error(exc) from None
    return issue_code(new, fabric=fabric, host=host, port=port).as_dict()


@fabric_router.get("/invites")
async def list_invites(
    _admin: InviteAdmin, api: Annotated[InviteApi, Depends(get_invite_api)]
) -> dict[str, Any]:
    try:
        rows = await api.store.list()
    except MeshForgeError as exc:
        raise _api_error(exc) from None
    return {"invites": [row.as_dict() for row in rows]}


@fabric_router.post("/invites/{invite_id}/revoke")
async def revoke_invite(
    invite_id: str,
    request: Request,
    _admin: InviteAdmin,
    api: Annotated[InviteApi, Depends(get_invite_api)],
) -> dict[str, Any]:
    if len(invite_id) > _MAX_ID_CHARS:  # checked after auth (D152)
        raise ApiError.invalid("invite_id is not an invite id")
    try:
        record = await api.store.revoke(
            invite_id, actor=_ACTOR, source=_source(request)
        )
    except MeshForgeError as exc:
        raise _api_error(exc) from None
    return record.as_dict()
