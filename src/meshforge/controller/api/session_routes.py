"""``/v1/session``: the dashboard's browser session (V0.8 G1, D170).

* ``POST /v1/session`` ``{name, password}`` (``application/json`` only, same
  origin only) -> ``200 {user, session_id, expires_at, idle_timeout_s}`` and
  two cookies (:mod:`meshforge.controller.auth.browser`). Same throttle and
  backoff as ``POST /v1/auth/login`` (D159): 429 + ``Retry-After``; wrong
  name / password / disabled user: one 401. The token appears only in the
  ``HttpOnly`` cookie, never in the body. A fresh token every time - a cookie
  the browser already had is ignored (no fixation).
* ``GET /v1/session`` - the current session (``401`` without one): who, when
  it ends (absolute and idle). The dashboard's "am I logged in".
* ``DELETE /v1/session`` - log out: the session is revoked server-side (its
  open streams close at once, D157) and both cookies are expired. Needs the
  CSRF header like every cookie mutation; a dead or absent session still gets
  its cookies cleared (``logged_out: false``).

Sessions are listed and revoked with the API tokens (``GET/DELETE
/v1/auth/tokens``, ``kind = browser_session``).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, ValidationError

from meshforge.common.errors import FailureType
from meshforge.controller.api.auth import (
    CurrentUser,
    authenticate,
    get_auth,
    request_source,
)
from meshforge.controller.api.auth_routes import AUTH_API_STATE_KEY, AuthApi
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel
from meshforge.controller.auth import browser
from meshforge.controller.auth.login import LoginRefusedError
from meshforge.controller.auth.passwords import MAX_PASSWORD_CHARS
from meshforge.controller.auth.users import (
    TokenRecord,
    UserNotFoundError,
    UserStoreError,
)
from meshforge.controller.store.models.auth import MAX_USER_NAME_CHARS, TokenKind

__all__ = ["SessionOut", "session_router"]

_MAX_BODY_BYTES: Final = 4096

session_router = APIRouter(prefix="/v1", tags=["session"])


class SessionIn(ApiModel):
    name: str = Field(min_length=1, max_length=MAX_USER_NAME_CHARS)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


class SessionUser(ApiModel):
    id: str
    name: str
    role: str


class SessionOut(ApiModel):
    """A browser session (never its token: that is only in the cookie)."""

    user: SessionUser
    session_id: str
    """The ``tok_...`` id it is listed and revoked under."""
    expires_at: str
    """Absolute end (``session_max_days``), ISO 8601."""
    idle_expires_at: str | None
    """When it ends if not used before (``session_idle_hours``)."""
    idle_timeout_s: int


class LogoutOut(ApiModel):
    logged_out: bool
    """False: there was no live session (the cookies are cleared anyway)."""


def _api(request: Request) -> AuthApi:
    api: AuthApi | None = getattr(request.app.state, AUTH_API_STATE_KEY, None)
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "user login is not configured")
    return api


Api = Annotated[AuthApi, Depends(_api)]


def _unavailable() -> ApiError:
    return ApiError(
        FailureType.NETWORK_ERROR,
        "the user database is unavailable; retry",
        headers={"Retry-After": "2"},
    )


def _cross_origin_refused() -> ApiError:
    return ApiError(
        FailureType.UNAUTHORIZED,
        "a dashboard login must come from the dashboard's own page",
        http_status=status.HTTP_403_FORBIDDEN,
        extra={"reason": "cross_origin"},
    )


async def _body(request: Request) -> SessionIn:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        # An HTML form cannot send JSON: no login CSRF from a hostile page.
        raise ApiError.invalid("a dashboard login is a JSON body (application/json)")
    declared = request.headers.get("content-length")
    if declared is not None and (
        not declared.isdigit() or int(declared) > _MAX_BODY_BYTES
    ):
        raise ApiError.invalid("the login body is too large")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > _MAX_BODY_BYTES:
            raise ApiError.invalid("the login body is too large")
    try:
        return SessionIn.model_validate_json(bytes(data))
    except ValidationError:
        raise ApiError.invalid(  # never echo the body: it holds a password
            "a dashboard login is {name, password}: name 1-64 characters, "
            f"password 1-{MAX_PASSWORD_CHARS} characters"
        ) from None


def _out(api: AuthApi, user: SessionUser, record: TokenRecord) -> SessionOut:
    return SessionOut(
        user=user,
        session_id=record.id,
        expires_at=record.expires_at.isoformat(),
        idle_expires_at=record.idle_expires_at.isoformat()
        if record.idle_expires_at
        else None,
        idle_timeout_s=api.users.session_idle_s,
    )


@session_router.post(
    "/session",
    summary="Dashboard login: name + password -> session cookie",
    response_model=SessionOut,
)
async def open_session(request: Request, api: Api) -> JSONResponse:
    if browser.cross_origin(request, require=False):
        raise _cross_origin_refused()
    body = await _body(request)
    client = request.client
    try:
        result = await api.login.open_session(
            body.name,
            body.password,
            host=client.host if client else None,
            source=request_source(request),
        )
    except LoginRefusedError as refused:
        if refused.retry_after_s is not None:
            raise ApiError(
                FailureType.RESOURCE_BUSY,
                refused.message,
                http_status=429,
                headers={"Retry-After": str(max(1, round(refused.retry_after_s)))},
                extra={"reason": refused.reason},
            ) from None
        raise ApiError(FailureType.UNAUTHORIZED, refused.message) from None
    except UserStoreError:
        raise _unavailable() from None
    principal = result.principal
    out = _out(
        api,
        SessionUser(
            id=str(principal.user_id), name=principal.name, role=principal.role.value
        ),
        result.record,
    )
    response = JSONResponse(out.model_dump(), headers={"Cache-Control": "no-store"})
    browser.set_session_cookies(
        response,
        result.token.get_secret_value(),
        max_age_s=api.login.policy.session_ttl_s,
    )
    return response


@session_router.get(
    "/session", summary="The current dashboard session", response_model=SessionOut
)
async def current_session(user: CurrentUser, api: Api) -> SessionOut:
    if user.token_kind is not TokenKind.BROWSER_SESSION or user.token_id is None:
        raise ApiError(
            FailureType.INVALID_INPUT,
            "this call is for the dashboard's session cookie; an API token has "
            "`GET /v1/auth/whoami`",
            extra={"reason": "not_a_session"},
        )
    try:
        record = await api.users.get_token(user.token_id)
    except UserStoreError:
        raise _unavailable() from None
    if record is None:  # revoked and gone between the two reads
        raise ApiError(FailureType.UNAUTHORIZED, "the session has ended")
    return _out(
        api,
        SessionUser(id=str(user.user_id), name=user.name, role=user.role.value),
        record,
    )


@session_router.delete("/session", summary="Dashboard logout", response_model=LogoutOut)
async def close_session(request: Request, api: Api) -> JSONResponse:
    # Raises 403 csrf for a live session without the header (fail closed).
    principal = await authenticate(request)
    logged_out = False
    if (
        principal is not None
        and principal.token_kind is TokenKind.BROWSER_SESSION
        and principal.token_id is not None
    ):
        try:
            _, already = await api.users.revoke_token(
                principal.token_id, by=principal, source=request_source(request)
            )
        except UserNotFoundError:
            already = True
        except UserStoreError:
            raise _unavailable() from None
        logged_out = not already
        streams = get_auth(request).live_streams
        if streams is not None:
            streams.recheck_now()  # D157: its event/log streams close at once
    response = JSONResponse(
        LogoutOut(logged_out=logged_out).model_dump(),
        headers={"Cache-Control": "no-store"},
    )
    browser.clear_session_cookies(response)
    return response
