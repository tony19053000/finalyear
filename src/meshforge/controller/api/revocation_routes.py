"""``POST /v1/nodes/{node_id}/revoke`` - take a node out of the fabric (V0.7
G3, roadmap 0.7.7a, D138).

Needs an **admin** (V0.7 G4 re-gate of D151: ``role=admin``, 403 for a user;
the legacy fabric token counts as an admin only while it is switched on).
Answers 200 with ``{node_id, state: "revoked", serial, already_revoked,
previous_state, session_killed, enforcement}`` - idempotent: revoking a revoked node is
200 with ``already_revoked: true`` and changes nothing. Unknown node -> 404;
a string that is not a node id -> 400 (checked after the token, D152 T-2);
database down -> 503 ``network_error`` (nothing was committed). Once the
revocation is committed the answer is 200 even if enforcing it right away
failed (D156 a): ``enforcement: "deferred"`` - the periodic sync retries it
within ``[controller.revocation] poll_interval_s`` and admission already
refuses the node from its database row.

The node's live session is cut before the response is sent (PERMISSION_DENIED,
``node_revoked``), its attempts take the node-loss path, and every agent is
told the new revocation set (:mod:`meshforge.controller.revocation`). There is
no un-revoke: the laptop comes back only under a new identity.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.ids import validate_node_id
from meshforge.controller.api.auth import AdminUser, request_source
from meshforge.controller.api.errors import ApiError
from meshforge.controller.revocation import (
    NodeNotFoundError,
    RevocationError,
    Revocations,
)

__all__ = ["REVOCATION_STATE_KEY", "revocation_router"]

REVOCATION_STATE_KEY: Final = "meshforge_revocations"
_MAX_ID_CHARS: Final = 64

revocation_router = APIRouter(prefix="/v1/nodes", tags=["nodes"])


def get_revocations(request: Request) -> Revocations:
    found: Revocations | None = getattr(request.app.state, REVOCATION_STATE_KEY, None)
    if found is None:
        raise ApiError(FailureType.CONFIG_ERROR, "revocation is not configured")
    return found


def _is_node_id(text: str) -> bool:
    if len(text) > _MAX_ID_CHARS:
        return False
    try:
        validate_node_id(text)
    except MeshForgeError:
        return False
    return True


@revocation_router.post("/{node_id}/revoke")
async def revoke_node(
    node_id: str,
    request: Request,
    admin: AdminUser,
    revocations: Annotated[Revocations, Depends(get_revocations)],
) -> dict[str, Any]:
    if not _is_node_id(node_id):  # checked after the token (D152 T-2)
        raise ApiError.invalid("node_id is not a node id (node_...)")
    try:
        outcome = await revocations.revoke(
            node_id, actor=admin.actor, source=request_source(request)
        )
    except NodeNotFoundError:
        raise ApiError.not_found("node") from None
    except RevocationError:
        raise ApiError(
            FailureType.NETWORK_ERROR, "the database is unavailable; retry"
        ) from None
    return outcome.as_dict()
