"""Explicit response and error models for the ``/v1`` routes that used to return
plain dicts (V0.8 G1, D171/D174): the OpenAPI document is the contract the
dashboard's TypeScript client is generated from, so every route declares what
it answers - and FastAPI checks each answer against it (``extra="forbid"``: a
field added in code but not here fails the request in every test, so the
contract cannot drift silently).

:data:`ERROR_RESPONSES` documents the one error envelope
(:mod:`meshforge.controller.api.errors`) for every status a route can return;
it is attached app-wide in :func:`meshforge.controller.http.create_http_app`.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import ConfigDict

from meshforge.common.errors import FailureType
from meshforge.controller.api.models import ApiModel

__all__ = [
    "ERROR_RESPONSES",
    "BlobCompleted",
    "BlobMissing",
    "BlobPartStored",
    "BlobParts",
    "BlobStored",
    "ErrorDetail",
    "ErrorEnvelope",
    "GcRequested",
    "InputList",
    "InputOut",
    "InputRegistered",
    "InputRemoved",
    "InputVersions",
    "InviteCreated",
    "InviteList",
    "InviteOut",
    "LoginOut",
    "MetricPoint",
    "NodeMetrics",
    "NodeRevoked",
    "PrepareStarted",
    "PrepareStatus",
    "SecretDeleted",
    "SecretList",
    "SecretOut",
    "SecretStored",
    "TokenList",
    "TokenOut",
    "TokenRevoked",
    "UserOut",
    "WhoAmI",
]


class ErrorDetail(ApiModel):
    """``code`` is a ``FailureType``; some errors add a machine ``reason`` (and
    a few other documented keys) next to it."""

    model_config = ConfigDict(extra="allow", frozen=True)

    code: FailureType
    message: str
    reason: str | None = None


class ErrorEnvelope(ApiModel):
    error: ErrorDetail


_ERROR_STATUSES: Final = {
    400: "Invalid input (`invalid_input`)",
    401: "No, or no longer a valid, credential (`unauthorized`)",
    403: "Not allowed: admin only, CSRF, foreign origin (`unauthorized`)",
    404: "Not found - also another user's object (`not_found`)",
    409: "Conflict (`conflict`, `no_feasible_plan`)",
    422: "Invalid parameters (`invalid_input`)",
    429: "Rate limited; see Retry-After (`resource_busy`)",
    503: "A dependency (the database) is unavailable; retry (`network_error`)",
}
ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    code: {"model": ErrorEnvelope, "description": text}
    for code, text in _ERROR_STATUSES.items()
}


# ------------------------------------------------------------------ auth


class UserOut(ApiModel):
    id: str | None
    name: str
    role: str


class LoginOut(ApiModel):
    token: str
    """The API token - shown once."""
    token_id: str
    expires_at: str
    user: UserOut


class WhoAmI(ApiModel):
    user: UserOut
    legacy: bool
    token_id: str | None
    token_expires_at: str | None


class TokenOut(ApiModel):
    id: str
    user_id: str
    user: str
    name: str
    state: str
    """``active`` | ``expired`` | ``revoked``."""
    current: bool
    created_at: str
    expires_at: str
    last_used_at: str | None
    revoked_at: str | None
    kind: str
    """``api`` | ``browser_session`` (V0.8 G1, D170)."""
    idle_expires_at: str | None


class TokenList(ApiModel):
    tokens: list[TokenOut]
    count: int


class TokenRevoked(TokenOut):
    already_revoked: bool


# ---------------------------------------------------------------- fabric


class InviteCreated(ApiModel):
    code: str
    """The join code - shown once."""
    invite_id: str
    address: str
    ca_pin: str
    expires_at: str
    replace_node_id: str | None


class InviteOut(ApiModel):
    id: str
    state: str
    created_by: str
    created_at: str
    expires_at: str
    used_at: str | None
    used_by_node: str | None
    revoked_at: str | None
    replace_node_id: str | None


class InviteList(ApiModel):
    invites: list[InviteOut]


class NodeRevoked(ApiModel):
    node_id: str
    state: str
    serial: str | None
    already_revoked: bool
    previous_state: str
    session_killed: bool
    enforcement: str
    """``immediate`` | ``deferred``."""


# --------------------------------------------------------------- secrets


class SecretStored(ApiModel):
    name: str
    created: bool


class SecretOut(ApiModel):
    name: str
    created_at: str
    updated_at: str


class SecretList(ApiModel):
    secrets: list[SecretOut]
    count: int


class SecretDeleted(ApiModel):
    name: str
    deleted: bool


# ------------------------------------------------------------------ data


class InputOut(ApiModel):
    name: str
    version: int
    ref: str
    tree_id: str
    size: int
    files: int
    pinned: bool
    created_at: str


class InputRegistered(InputOut):
    created: bool


class InputRemoved(InputOut):
    removed: bool


class InputList(ApiModel):
    inputs: list[InputOut]
    count: int


class InputVersions(ApiModel):
    name: str
    versions: list[InputOut]


# --------------------------------------------------------------- prepare


class PrepareStarted(ApiModel):
    request_id: str
    node_id: str


class PrepareStatus(ApiModel):
    request_id: str
    node_id: str
    created_at: str
    updated_at: str | None
    images: list[str]
    envs: int
    done: bool
    ok: bool
    items: list[dict[str, Any]]


class GcRequested(ApiModel):
    node_id: str
    requested: bool


# ----------------------------------------------------------------- blobs


class BlobStored(ApiModel):
    digest: str
    size: int
    created: bool


class BlobMissing(ApiModel):
    missing: list[str]


class BlobPartStored(ApiModel):
    digest: str
    index: int
    stored: bool


class BlobParts(ApiModel):
    digest: str
    size: int
    chunk_size: int
    received: list[int]
    complete: bool


class BlobCompleted(ApiModel):
    digest: str
    size: int
    created: bool
    credited: bool


# --------------------------------------------------------------- metrics

MIN_METRICS_WINDOW_S: Final = 10
MAX_METRICS_WINDOW_S: Final = 300


class GpuPoint(ApiModel):
    index: int
    util_percent: float
    vram_used_bytes: int
    vram_total_bytes: int


class MetricPoint(ApiModel):
    """One heartbeat sample - the same fields as a ``node.metrics`` event."""

    node_id: str
    observed_at: str
    """The agent's wall clock when it measured."""
    age_s: float
    """How long ago the controller received it (its monotonic clock)."""
    cpu_percent: float
    ram_used_bytes: int
    ram_total_bytes: int
    gpus: list[GpuPoint]


class NodeMetrics(ApiModel):
    node_id: str
    window_s: int
    samples: tuple[MetricPoint, ...]
    """Oldest first."""
    count: int
