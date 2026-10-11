"""Who may call what on ``/v1`` (V0.7 G4, D141/D148; was D44 / ADR-0014).

One authentication path, enforced here and nowhere else:

1. **Every route needs a user** - an app-wide dependency
   (:func:`guard_requests`), so a route added later cannot forget it (D148:
   node, session, network and resource reads too). Exempt: ``/healthz``,
   ``POST /v1/auth/login`` (it *is* the way in), and the blob routes, which check
   their own credentials (a user token *or* an agent's per-attempt fetch token /
   upload grant + node certificate, :mod:`.blob_routes`). WebSocket routes
   authenticate their own handshake (:func:`authenticate`) and then register
   with :attr:`ApiAuth.live_streams`, which closes them when the token stops
   being live (D157).
2. A **user** is an ``Authorization: Bearer <API token>`` whose ``api_token`` row
   is live and whose user is enabled (:meth:`UserDirectory.authenticate`, one
   database read per request - a revoke, expiry or disable takes effect on the
   next request). The legacy **fabric token** is accepted only with
   ``[controller.auth] legacy_fabric_token = true`` (default false) and then
   acts as an admin, with a warning log and an audit row (D141).
3. **Authorization** reads only the :class:`~meshforge.controller.auth.principal.Principal`:
   :data:`AdminUser` for invites, node revoke and other admin calls (403 for a
   user), :meth:`Principal.may_see_job` for every job route (another user's job
   is 404, exactly like a job that does not exist).

A missing, malformed, unknown, expired or revoked token and a disabled user all
get the same ``unauthorized`` envelope (401, ``WWW-Authenticate: Bearer``) with
one fixed message: the response never says *which* part was wrong and never
echoes what was sent. A database failure while authenticating is 503 - never a
silent "anonymous".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, Protocol

from fastapi import Depends, Request, status
from starlette.requests import HTTPConnection

from meshforge.common.api_token import is_api_token
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.api.errors import ApiError
from meshforge.controller.auth.principal import LEGACY_PRINCIPAL, Principal

if TYPE_CHECKING:
    from meshforge.controller.auth.live_streams import LiveStreams
    from meshforge.controller.blobs import FetchTokens
    from meshforge.controller.fabric_token import FabricTokenVerifier
    from meshforge.controller.http_tls import NodeCertPolicy
    from meshforge.controller.upload_grants import UploadGrants

log = get_logger(__name__)

__all__ = [
    "AUTH_STATE_KEY",
    "BLOB_ROUTE_PREFIX",
    "MAX_AUTHORIZATION_CHARS",
    "OPEN_ROUTES",
    "AdminUser",
    "ApiAuth",
    "CurrentUser",
    "UserDirectory",
    "agent_credential_ok",
    "authenticate",
    "bearer_token",
    "current_user",
    "forbidden",
    "get_auth",
    "guard_requests",
    "request_source",
    "require_admin",
    "unauthorized",
]

AUTH_STATE_KEY: Final = "meshforge_auth"
_PRINCIPAL_STATE_KEY: Final = "meshforge_principal"
_UNSET: Final = object()
BLOB_ROUTE_PREFIX: Final = "/v1/blobs/{"
"""Routes under this template check their own credentials (user or agent)."""
OPEN_ROUTES: Final = frozenset({("GET", "/healthz"), ("POST", "/v1/auth/login")})
"""(method, route template) pairs that need no user (D148: only these)."""
MAX_AUTHORIZATION_CHARS: Final = 512
"""An ``Authorization`` header longer than this is refused unread (a token is 48)."""
_UNAUTHORIZED_MESSAGE: Final = (
    "this call needs an API token: run `compute login` (or send "
    "'Authorization: Bearer <token>'); an expired or revoked token needs a new login"
)
_FORBIDDEN_MESSAGE: Final = "this call needs an admin account"


class UserDirectory(Protocol):
    """What the API needs from the user store
    (:class:`~meshforge.controller.auth.users.UserStore`)."""

    async def authenticate(self, token: str) -> Principal | None: ...

    async def note_legacy_use(self, source: str | None) -> None: ...

    async def owns_output(self, user_id: str, digest_hex: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ApiAuth:
    """What the API checks credentials against. No directory = refuse all users."""

    verifier: FabricTokenVerifier | None
    """The fabric token (D44) - consulted only when ``legacy_fabric_token``."""
    fetch_tokens: FetchTokens | None = None
    """Per-attempt blob tokens (D48, download-only); accepted on ``/v1/blobs``."""
    upload_grants: UploadGrants | None = None
    """Per-attempt upload grants (D73); accepted on ``PUT /v1/blobs`` only."""
    node_certs: NodeCertPolicy | None = None
    """V0.7 G3 (D139, mtls mode): a fetch token or upload grant is accepted
    only with the TLS client certificate of the node it was minted for (two
    locks); ``None`` (insecure-dev) = the token alone."""
    users: UserDirectory | None = None
    """V0.7 G4 (D141): API tokens -> users; ``None`` = no user is accepted."""
    legacy_fabric_token: bool = False
    """``[controller.auth] legacy_fabric_token``: accept the fabric token as an
    admin (one release of overlap; default off)."""
    live_streams: LiveStreams | None = None
    """D157 T-1: open log WebSockets register here and are closed when their
    token stops being live (``None`` = authenticated at the handshake only)."""


def agent_credential_ok(
    conn: HTTPConnection, auth: ApiAuth, node_id: str | None
) -> bool:
    """D139: in mtls mode the request carries a valid node certificate, and
    it is the certificate of ``node_id`` (the attempt's node) when known."""
    if auth.node_certs is None:
        return True
    from meshforge.controller.http_tls import request_node_cert
    from meshforge.controller.session.peer_cert import PeerCertError

    try:
        cert = request_node_cert(conn, auth.node_certs)
    except PeerCertError as error:
        log.warning("controller.api.node_cert_refused", reason=error.reason.value)
        return False
    if node_id is not None and cert.node_id != node_id:
        log.warning(
            "controller.api.node_cert_refused",
            reason="other_node",
            node_id=cert.node_id,
            token_node_id=node_id,
        )
        return False
    return True


def get_auth(request: HTTPConnection) -> ApiAuth:
    auth = getattr(request.app.state, AUTH_STATE_KEY, None)
    return auth if isinstance(auth, ApiAuth) else ApiAuth(verifier=None)


def unauthorized() -> ApiError:
    return ApiError(
        FailureType.UNAUTHORIZED,
        _UNAUTHORIZED_MESSAGE,
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden() -> ApiError:
    """403 for an authenticated user who is not an admin."""
    return ApiError(
        FailureType.UNAUTHORIZED,
        _FORBIDDEN_MESSAGE,
        http_status=status.HTTP_403_FORBIDDEN,
    )


def bearer_token(request: HTTPConnection) -> str | None:
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


def request_source(conn: HTTPConnection) -> str | None:
    """``host:port`` of the client, for audit rows."""
    client = conn.client
    return f"{client.host}:{client.port}" if client else None


async def authenticate(conn: HTTPConnection) -> Principal | None:
    """The caller (HTTP request or WebSocket handshake), or ``None``.

    Cached on the connection, so the app-wide guard and a route's own
    dependency cost one database read together. Raises 503 when the user
    database cannot be read (fail closed).
    """
    cached: object = getattr(conn.state, _PRINCIPAL_STATE_KEY, _UNSET)
    if cached is None or isinstance(cached, Principal):
        return cached
    principal = await _resolve(conn)
    setattr(conn.state, _PRINCIPAL_STATE_KEY, principal)
    return principal


async def _resolve(conn: HTTPConnection) -> Principal | None:
    auth = get_auth(conn)
    token = bearer_token(conn)
    if token is None:
        return None
    if is_api_token(token) and auth.users is not None:
        try:
            principal = await auth.users.authenticate(token)
        except MeshForgeError:
            log.warning("controller.auth.directory_unavailable")
            raise ApiError(
                FailureType.NETWORK_ERROR,
                "the user database is unavailable; retry",
                headers={"Retry-After": "2"},
            ) from None
        if principal is not None:
            return principal
    if (
        auth.legacy_fabric_token
        and auth.verifier is not None
        and auth.verifier.verify(token)
    ):
        if auth.users is not None:
            await auth.users.note_legacy_use(request_source(conn))
        return LEGACY_PRINCIPAL
    return None


async def current_user(request: Request) -> Principal:
    """Dependency: the authenticated caller, or 401."""
    principal = await authenticate(request)
    if principal is None:
        raise unauthorized()
    return principal


async def require_admin(request: Request) -> Principal:
    """Dependency: an admin caller; 401 without a user, 403 for a non-admin."""
    principal = await current_user(request)
    if not principal.is_admin:
        log.warning(
            "controller.auth.admin_required",
            user_id=principal.user_id,
            route=getattr(request.scope.get("route"), "path", None),
        )
        raise forbidden()
    return principal


CurrentUser = Annotated[Principal, Depends(current_user)]
AdminUser = Annotated[Principal, Depends(require_admin)]


async def guard_requests(request: HTTPConnection) -> None:
    """App-wide dependency: every route needs a user (D148), except
    :data:`OPEN_ROUTES` and the blob routes (their own credentials). A WebSocket
    route authenticates its own handshake - a refusal there is a close."""
    if request.scope.get("type") == "websocket":
        return
    # Dependencies run after routing, so the matched route's *template* is known
    # (a raw-path prefix test could be fooled by an unnormalised URL).
    route_path = getattr(request.scope.get("route"), "path", "")
    if not isinstance(route_path, str):
        route_path = ""
    if route_path.startswith(BLOB_ROUTE_PREFIX):
        return
    method = request.scope.get("method")
    if method == "HEAD":
        method = "GET"
    if (method, route_path) in OPEN_ROUTES:
        return
    if await authenticate(request) is None:
        raise unauthorized()
