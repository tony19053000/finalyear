"""``/v1/auth``: log in, list and revoke API tokens, who am I (V0.7 G4, D141).

* ``POST /v1/auth/login`` ``{name, password, token_name?}`` -> ``{token, token_id,
  expires_at, user}``. The only route that needs no token. Per-source and
  per-user backoff (:mod:`meshforge.controller.auth.login`): 429 +
  ``Retry-After``. Wrong name / wrong password / disabled user: one 401. The
  body is read with a hard cap and is never logged; the token appears once, in
  this response.
* ``GET /v1/auth/whoami`` - the caller (user id, name, role, token id/expiry).
* ``GET /v1/auth/tokens`` - the caller's tokens (``?all=true``: every user's,
  admin only). Never a token or its hash.
* ``DELETE /v1/auth/tokens/{token_id}`` - revoke one (``compute logout`` /
  ``compute tokens revoke``); another user's token is 404 like an unknown id;
  an admin may revoke any. Idempotent (``already_revoked``). Log streams
  opened with it are closed at once (D157).

The legacy fabric token has no user and therefore no tokens: these routes
answer it 400 ``legacy_token``.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, SecretStr, ValidationError

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_api_token_id
from meshforge.controller.api.auth import (
    CurrentUser,
    forbidden,
    get_auth,
    request_source,
)
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel
from meshforge.controller.auth.login import LoginRefusedError, LoginService
from meshforge.controller.auth.passwords import MAX_PASSWORD_CHARS
from meshforge.controller.auth.principal import Principal
from meshforge.controller.auth.users import (
    UserNotFoundError,
    UserStore,
    UserStoreError,
)
from meshforge.controller.store.models.auth import (
    MAX_TOKEN_NAME_CHARS,
    MAX_USER_NAME_CHARS,
)

__all__ = ["AUTH_API_STATE_KEY", "AuthApi", "auth_router"]

AUTH_API_STATE_KEY: Final = "meshforge_auth_api"
_MAX_BODY_BYTES: Final = 4096
_MAX_ID_CHARS: Final = 40

auth_router = APIRouter(prefix="/v1/auth", tags=["auth"])


class AuthApi:
    """What the ``/v1/auth`` routes need; put on ``app.state``."""

    def __init__(self, *, users: UserStore, login: LoginService) -> None:
        self.users = users
        self.login = login


def get_auth_api(request: Request) -> AuthApi:
    api: AuthApi | None = getattr(request.app.state, AUTH_API_STATE_KEY, None)
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "user login is not configured")
    return api


Api = Annotated[AuthApi, Depends(get_auth_api)]


class LoginIn(ApiModel):
    name: str = Field(min_length=1, max_length=MAX_USER_NAME_CHARS)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    token_name: str = Field(
        default="compute", min_length=1, max_length=MAX_TOKEN_NAME_CHARS
    )


async def _login_body(request: Request) -> LoginIn:
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
        return LoginIn.model_validate_json(bytes(data))
    except ValidationError:
        # Never echo the body: it holds a password.
        raise ApiError.invalid(
            "a login is {name, password, token_name?}: name 1-64 characters, "
            f"password 1-{MAX_PASSWORD_CHARS} characters"
        ) from None


def _unavailable() -> ApiError:
    return ApiError(
        FailureType.NETWORK_ERROR,
        "the user database is unavailable; retry",
        headers={"Retry-After": "2"},
    )


def _user_out(principal: Principal) -> dict[str, Any]:
    return {
        "id": principal.user_id,
        "name": principal.name,
        "role": principal.role.value,
    }


@auth_router.post("/login", summary="Log in: name + password -> API token")
async def login(request: Request, api: Api) -> dict[str, Any]:
    body = await _login_body(request)
    client = request.client
    try:
        result = await api.login.login(
            body.name,
            body.password,
            host=client.host if client else None,
            source=request_source(request),
            token_name=body.token_name,
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
        raise ApiError(
            FailureType.UNAUTHORIZED,
            refused.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except UserStoreError:
        raise _unavailable() from None
    return {
        "token": result.token.get_secret_value(),
        "token_id": result.record.id,
        "expires_at": result.record.expires_at.isoformat(),
        "user": _user_out(result.principal),
    }


def _not_legacy(principal: Principal) -> str:
    if principal.user_id is None:
        raise ApiError(
            FailureType.INVALID_INPUT,
            "the legacy fabric token has no user and no API tokens; run "
            "`compute login`",
            extra={"reason": "legacy_token"},
        )
    return principal.user_id


@auth_router.get("/whoami", summary="The calling user and token")
async def whoami(user: CurrentUser, api: Api) -> dict[str, Any]:
    out: dict[str, Any] = {
        "user": _user_out(user),
        "legacy": user.legacy,
        "token_id": user.token_id,
        "token_expires_at": None,
    }
    if user.token_id is not None:
        try:
            record = await api.users.get_token(user.token_id)
        except UserStoreError:
            raise _unavailable() from None
        if record is not None:
            out["token_expires_at"] = record.expires_at.isoformat()
    return out


@auth_router.get("/tokens", summary="Your API tokens (admin: ?all=true)")
async def list_tokens(
    user: CurrentUser,
    api: Api,
    all_users: Annotated[bool, Query(alias="all")] = False,
) -> dict[str, Any]:
    if all_users and not user.is_admin:
        raise forbidden()
    user_id = None if all_users else _not_legacy(user)
    try:
        records = await api.users.list_tokens(user_id)
    except UserStoreError:
        raise _unavailable() from None
    tokens = [r.as_dict(current_token_id=user.token_id) for r in records]
    return {"tokens": tokens, "count": len(tokens)}


@auth_router.delete("/tokens/{token_id}", summary="Revoke one API token")
async def revoke_token(
    token_id: str, request: Request, user: CurrentUser, api: Api
) -> dict[str, Any]:
    if len(token_id) > _MAX_ID_CHARS:
        raise ApiError.invalid("token_id is not a token id (tok_...)")
    try:
        checked = str(validate_api_token_id(token_id))
    except InvalidIdError:
        raise ApiError.invalid("token_id is not a token id (tok_...)") from None
    if not user.is_admin:
        _not_legacy(user)
    try:
        record, already = await api.users.revoke_token(
            checked, by=user, source=request_source(request)
        )
    except UserNotFoundError:
        raise ApiError.not_found("token") from None
    except UserStoreError:
        raise _unavailable() from None
    streams = get_auth(request).live_streams
    if streams is not None:
        streams.recheck_now()  # D157: its open log streams close at once
    return {
        **record.as_dict(current_token_id=user.token_id),
        "already_revoked": already,
    }
