"""Who may call what on ``/v1`` (brief D44, ADR-0014 - interim until V0.7).

Three rules, enforced here and nowhere else:

1. **Every mutating request** (anything but ``GET``/``HEAD``/``OPTIONS``) needs the
   fabric token - an app-wide dependency (:func:`guard_mutations`), so a mutating
   route added later cannot forget it.
2. **Job-scoped reads** (``/v1/jobs*``: logs contain user output) need the fabric
   token too - :data:`FabricTokenRequired` on those routes.
3. **Blob calls** accept the fabric token *or* a per-attempt fetch token scoped to
   the digests it names (:mod:`meshforge.controller.api.blob_routes`).

The node/session GETs of D14 stay open. The token travels only as
``Authorization: Bearer <token>`` (never in a URL), is compared in constant time,
and a missing, malformed or wrong value is the typed envelope ``unauthorized``
(401, ``WWW-Authenticate: Bearer``) with one fixed message - the response never
says *which* part was wrong and never echoes what was sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import Depends, Request

from meshforge.common.errors import FailureType
from meshforge.controller.api.errors import ApiError

if TYPE_CHECKING:
    from meshforge.controller.blobs import FetchTokens
    from meshforge.controller.fabric_token import FabricTokenVerifier
    from meshforge.controller.upload_grants import UploadGrants

__all__ = [
    "AUTH_STATE_KEY",
    "BLOB_ROUTE_PREFIX",
    "MAX_AUTHORIZATION_CHARS",
    "SAFE_METHODS",
    "ApiAuth",
    "FabricTokenRequired",
    "bearer_token",
    "get_auth",
    "guard_mutations",
    "require_fabric_token",
    "unauthorized",
]

AUTH_STATE_KEY: Final = "meshforge_auth"
BLOB_ROUTE_PREFIX: Final = "/v1/blobs/{"
"""Routes under this template check their own credentials (fabric or fetch token)."""
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_AUTHORIZATION_CHARS: Final = 512
"""An ``Authorization`` header longer than this is refused unread (a token is 43)."""
_UNAUTHORIZED_MESSAGE: Final = (
    "this call needs the fabric token: send 'Authorization: Bearer <token>' "
    "(`meshforge-controller token show` on the controller laptop prints it)"
)


@dataclass(frozen=True, slots=True)
class ApiAuth:
    """What the API checks credentials against. ``None`` verifier = refuse all."""

    verifier: FabricTokenVerifier | None
    fetch_tokens: FetchTokens | None = None
    """Per-attempt blob tokens (D48, download-only); accepted on ``/v1/blobs``."""
    upload_grants: UploadGrants | None = None
    """Per-attempt upload grants (D73); accepted on ``PUT /v1/blobs`` only."""


def get_auth(request: Request) -> ApiAuth:
    auth = getattr(request.app.state, AUTH_STATE_KEY, None)
    return auth if isinstance(auth, ApiAuth) else ApiAuth(verifier=None)


def unauthorized() -> ApiError:
    return ApiError(
        FailureType.UNAUTHORIZED,
        _UNAUTHORIZED_MESSAGE,
        headers={"WWW-Authenticate": "Bearer"},
    )


def bearer_token(request: Request) -> str | None:
    """The token of ``Authorization: Bearer <token>``, or ``None`` if absent/malformed."""
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None  # none, or an ambiguous repeat
    header = values[0]
    if len(header) > MAX_AUTHORIZATION_CHARS:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def _fabric_ok(auth: ApiAuth, token: str | None) -> bool:
    return auth.verifier is not None and auth.verifier.verify(token)


async def require_fabric_token(request: Request) -> None:
    """Dependency: the request carries the current fabric token, or 401."""
    if not _fabric_ok(get_auth(request), bearer_token(request)):
        raise unauthorized()


async def guard_mutations(request: Request) -> None:
    """App-wide dependency: every non-safe method needs the fabric token.

    Blob uploads are exempt here because their router checks the fabric token
    *or* a scoped fetch token itself (D48); every other mutation needs the
    fabric token.
    """
    if request.method in SAFE_METHODS:
        return
    # Dependencies run after routing, so the matched route's *template* is known
    # (a raw-path prefix test could be fooled by an unnormalised URL).
    route_path = getattr(request.scope.get("route"), "path", "")
    if isinstance(route_path, str) and route_path.startswith(BLOB_ROUTE_PREFIX):
        return
    await require_fabric_token(request)


FabricTokenRequired = Annotated[None, Depends(require_fabric_token)]
